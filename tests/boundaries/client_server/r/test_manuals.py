"""Installed manuals share a persistent, navigable Markdown representation."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.checkpoints import wait_for_path
from support.native import LOADER_VARIABLE, build_interposer
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import FIXTURES, r_test_environment
from support.records import Transcript
from support.requirements import POSIX, R, UNPRIVILEGED, command, requires
from support.resolvers import bare_runtime_environment


@contextmanager
def manual_client(
    binary: Path,
    execution: Execution,
    *,
    installed: bool = False,
    manuals: Path = FIXTURES / "r_manuals",
    doc_component: str = "doc",
    cache_race: str = "",
) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory(prefix="console-manuals-café#-") as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        environment, _ = r_test_environment()
        library = root / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        if cache_race:
            environment[LOADER_VARIABLE] = str(build_interposer(root, cache_race))
        environment.update(
            MCP_CONSOLE_LANGUAGES="r", XDG_CACHE_HOME=str(root / "cache")
        )
        doc = root / doc_component
        if not installed:
            shutil.copytree(manuals, doc / "manual")
            # A default manual request must prefer HTML when PDF also exists.
            (doc / "manual/R-exts.pdf").write_text("PDF viewer sentinel\n")
            (doc / "COPYING").write_text("Plain text still uses the pager.\n")
            environment["CONSOLE_TEST_DOC"] = str(doc)
        profile = workspace / ".Rprofile"
        profile.write_text(
            # fmt: r
            code(r"""
                options(
                  browser = function(...) stop("external browser invoked"),
                  pdfviewer = function(...) stop("PDF viewer invoked")
                )
                if (nzchar(Sys.getenv("CONSOLE_TEST_DOC"))) {
                  Sys.setenv(R_DOC_DIR = Sys.getenv("CONSOLE_TEST_DOC"))
                }
                """),
            encoding="utf-8",
        )
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                *(
                    (
                        "-c",
                        f"environment.{LOADER_VARIABLE}={json.dumps(environment[LOADER_VARIABLE])}",
                    )
                    if cache_race
                    else ()
                ),
                *(
                    ("--writable-root", str(workspace))
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            environment,
            current_directory=workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            yield client, root


def normalized_paths(output: str, root: Path) -> str:
    text = output.replace(str(root), "<fixture>").replace(root.as_posix(), "<fixture>")
    # Normalize native cache paths without changing backslashes in manual examples.
    text = re.sub(
        r"(?m)^(Cache: |Complete Markdown: )(.*)$",
        lambda match: match[1] + match[2].replace("\\", "/"),
        text,
    )
    # The identity includes the running R version and the native source path.
    text = re.sub(r"/manuals/[a-f0-9]{64}", "/manuals/<identity>", text)
    text = re.sub(r"(?m)^R version \d[^\n]*$", "R version <installed>", text)
    return text


def normalize_paths(client: McpClient, root: Path) -> str:
    output = last_tool_text(client)
    assert "[output omitted:" not in output, output
    client.transcript[-1]["result"]["content"][0]["text"] = normalized_paths(
        output, root
    )
    return output


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_navigation(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        output = normalize_paths(client, root)
        assert "Writing R Extensions" in output, output
        assert "1 Small sections" in output, output
        assert 'Read: `RShowDoc("R-exts#original-anchor")`' in output, output
        assert ".console$manual" in output, output
        assert "¶" not in output, output
        client.send(r='RShowDoc("R-exts#SEC_Contents")')
        output = normalize_paths(client, root)
        assert "Grouped A" in output, output
        client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
        output = normalize_paths(client, root)
        assert "1.2 Grouped B" in output and "café λ" in output, output
        client.send(r='RShowDoc("R-exts#Grouped-A")')
        output = normalize_paths(client, root)
        assert "1.1 Grouped A" in output and "1.2 Grouped B" not in output, output
        client.expect(
            "capture, native return, and plain pager retained\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(
                  value <- withVisible(utils::RShowDoc("R-exts#Grouped-B"))
                )
                a <- capture.output(RShowDoc("R-exts#Grouped-B"))
                b <- capture.output(utils::RShowDoc("R-exts#Grouped-B"))
                stopifnot(
                  !value$visible,
                  identical(value$value, file.path(R.home("doc"), "manual", "R-exts.html")),
                  identical(a, b),
                  any(grepl("café λ", a, fixed = TRUE)),
                  identical(
                    capture.output(RShowDoc("COPYING")),
                    "Plain text still uses the pager."
                  )
                )
                cat("capture, native return, and plain pager retained\n")
                """),
        )
        normalize_paths(client, root)
        client.send(r='RShowDoc("R-exts#absent-anchor")')
        assert "Unknown manual anchor" in normalize_paths(client, root)
        client.expect(
            "later browser override retained\n",
            # fmt: r
            r=code(r"""
                options(browser = function(url, ...) cat("later browser override retained\n"))
                utils::RShowDoc("R-exts#Grouped-B")
                """),
        )
        (root / "doc/manual/R-exts.html").unlink()
        client.expect(
            "missing HTML does not invoke a viewer\n",
            # fmt: r
            r=code(r"""
                error <- tryCatch(utils::RShowDoc("R-exts"), error = conditionMessage)
                stopifnot(identical(error, "document not found"))
                cat("missing HTML does not invoke a viewer\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_search_and_ranges(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='.console$manual_search("needle", limit = 2L)')
        output = normalize_paths(client, root)
        assert "2 Needle examples" in output, output
        assert "manual_search" in output and "start = 3" in output, output
        client.send(r='.console$manual_search("NEEDLE", start = 3L, limit = 2L)')
        output = normalize_paths(client, root)
        assert "Grouped" in output, output
        client.send(r='.console$manual("R-exts#Oversized-example")')
        output = normalize_paths(client, root)
        assert "start = " in output and "Continue:" in output, output
        command = re.search(r"Continue: (.+)", output)[1]
        client.send(r=command)
        continued = normalize_paths(client, root)
        assert "code-line-" in continued, continued
        assert "code-line-000" not in continued, continued
        assert (
            "Location: Writing R Extensions > 2 Needle examples > 2.2 Oversized example"
            in continued
        ), continued
        client.expect(
            "bounded reading and capture retained\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts#Oversized-example"))
                start <- grep(
                  "code-line-000",
                  readLines(path, encoding = "UTF-8"),
                  fixed = TRUE
                )
                a <- capture.output(.console$manual(
                  "R-exts#Oversized-example",
                  start = start,
                  lines = 4L
                ))
                stopifnot(
                  sum(grepl("code-line-", a, fixed = TRUE)) == 4L,
                  sum(a == "```") == 2L
                )
                b <- capture.output(.console$manual_search("absent search phrase"))
                stopifnot(any(grepl("No matches", b, fixed = TRUE)))
                cat("bounded reading and capture retained\n")
                """),
        )
        normalize_paths(client, root)
        for query in ("code-line-159", "wide-end"):
            client.send(r=f'.console$manual_search("{query}")')
            output = normalize_paths(client, root)
            assert any(
                query in line
                for line in output.splitlines()
                if line.startswith("  ") and not line.startswith("  Read:")
            ), output
            command = re.search(r"  Read: (.+)", output)[1]
            client.send(r=command)
            matched = normalize_paths(client, root)
            assert query in matched, matched
        client.expect(
            "search queries have a UTF-8 byte bound\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(
                  search_error <- tryCatch(
                    .console$manual_search(strrep("λ", 600L)),
                    error = conditionMessage
                  )
                )
                stopifnot(identical(
                  search_error,
                  "Manual search queries must be at most 1024 UTF-8 bytes"
                ))
                discard <- capture.output(
                  command_error <- tryCatch(
                    .console$manual_search(strrep("\u001b", 400L)),
                    error = conditionMessage
                  )
                )
                stopifnot(identical(
                  command_error,
                  "Manual search query must fit a 2048-byte continuation command"
                ))
                cat("search queries have a UTF-8 byte bound\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_search_budget(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        command = '.console$manual_search("Budgetmatch", limit = 20L)'
        seen = set()
        for _ in range(12):
            client.send(r=command)
            output = normalize_paths(client, root)
            assert len(output.encode("utf-8")) < 8192
            matches = re.findall(r"  Read: (.+)", output)
            lower, upper = map(int, re.search(r"results (\d+)–(\d+)", output).groups())
            assert upper - lower + 1 == len(matches), output
            assert not seen.intersection(matches), output
            seen.update(matches)
            continuation = re.search(r"Continue: (.+)", output)
            if not continuation:
                break
            command = continuation[1]
        assert len(seen) == 12, seen
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_search_visible_prose(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution, manuals=FIXTURES / "r_manuals_search") as (
        client,
        root,
    ):
        for query in (
            "Follow back to the extensions manual",
            "A formatted café λ and inline code reference",
            "literal *stars* and [link](destination)",
            "late formatted café λ and linked reference together",
            "literal | pipe",
            "A formatted table reference",
        ):
            client.send(
                r=f".console$manual_search({json.dumps(query, ensure_ascii=False)})"
            )
            output = normalize_paths(client, root)
            assert "1 matching sections" in output, output
            assert query in output, output
            command = re.search(r"  Read: (.+)", output)[1]
            client.send(r=command)
            preview = normalize_paths(client, root)
            assert "Writing R Extensions" in preview, preview
            if "late" in query:
                assert '.console$manual("R-exts#Deep-reference", start = ' in command
                assert (
                    "late formatted café λ" in preview and "linked reference" in preview
                ), preview
            if "table" in query:
                assert "formatted table" in preview, preview
            if "pipe" in query:
                assert "literal \\| pipe" in preview, preview
        client.send(r='.console$manual_search("destination")')
        assert "1 matching sections" in normalize_paths(client, root), (
            "literal code stays searchable"
        )
        client.send(r='.console$manual_search("#Shared-reference")')
        assert "No matches" in normalize_paths(client, root)
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_long_unicode_line(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='.console$manual("R-exts#Long-line")')
        output = normalize_paths(client, root)
        command = re.search(r"Continue: (.+)", output)[1]
        seen = ""
        while command:
            client.send(r=command)
            output = normalize_paths(client, root)
            assert len(output.encode("utf-8")) < 8192
            assert "λé" in output or "After the wide line." in output, output
            command_match = re.search(r"Continue: (.+)", output)
            command = command_match[1] if command_match else ""
            seen += (
                output.split("\n\n", 1)[1].split("\nContinue:", 1)[0].replace("\n", "")
            )
        assert seen.count("λ") == 3500 and seen.count("é") == 3500, seen
        client.send(r='.console$manual("R-exts#Code-anchors")')
        output = normalize_paths(client, root)
        assert "\n````\n   ```r\n" in output, output
        assert '<a id="Grouped-B"></a>' in output, output
        return client.finish()


@requires(R)
@executions(SANDBOXED)
def test_manual_cache_write_scope(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.expect(
            "manual cache is writable; its parent is read-only\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual())
                parent <- dirname(dirname(dirname(path)))
                stopifnot(!suppressWarnings(file.create(file.path(parent, "write-sentinel"))))
                cat("manual cache is writable; its parent is read-only\n")
                """),
        )
        assert not (root / "cache/mcp-console/write-sentinel").exists()
        return client.finish()


def cache_entries(root: Path) -> list[Path]:
    cache = root / "cache/mcp-console/manuals"
    # Native sandboxes can add protected metadata mounts at a writable root.
    # The public cache entries are the source-identity directories.
    entries = sorted(p for p in cache.iterdir() if re.fullmatch("[a-f0-9]{64}", p.name))
    assert not list(cache.glob(".*.incomplete")), (
        "publication left an incomplete collection"
    )
    assert all((p / "manifest.json").is_file() for p in entries), entries
    return entries


def completed_collection(root: Path) -> Path:
    entries = cache_entries(root)
    assert entries, "no completed manual collection"
    return entries[-1]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_single_document_previews(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts#Small-sections")')
        output = normalize_paths(client, root)
        collection = completed_collection(root)
        document = collection / "R-exts.md"
        assert document.is_file(), "each manual must have one complete Markdown file"
        assert {p.name for p in collection.glob("*.md")} == {
            "index.md",
            "R-exts.md",
            "R-lang.md",
        }
        assert "1.1 Grouped A" in output and "1.2 Grouped B" in output, output
        manifest = json.loads((collection / "manifest.json").read_text())
        manual = next(m for m in manifest["manuals"] if m["name"] == "R-exts")
        text = document.read_text(encoding="utf-8")
        assert text.count("\n# Writing R Extensions\n") == 1
        for anchor, location in manual["anchors"].items():
            assert f'<a id="{anchor}"></a>' in text.splitlines()[location["line"] - 1]
        client.send(r='RShowDoc("R-exts#Large-section")')
        output = normalize_paths(client, root)
        assert len(re.findall(r"paragraph-\d+", output)) == len(
            re.findall(r"without\s+overlapping\s+any\s+body\s+text[.]", output)
        ), "ordinary paragraphs must remain complete within a preview"
        client.send(r='utils::RShowDoc("R-exts#Oversized-example")')
        pages = []
        for _ in range(20):
            output = normalize_paths(client, root)
            assert len(output.encode("utf-8")) < 8192, output
            assert "File: R-exts.md" in output, output
            if "code-line-" in output:
                assert len(re.findall(r"(?m)^```$", output)) == 2, output
            pages.append(output.split("\n\n", 1)[1].split("\nContinue:", 1)[0])
            continuation = re.search(r"Continue: (.+)", output)
            if not continuation:
                break
            assert '"R-exts#Oversized-example"' in continuation[1], continuation[1]
            client.send(r=continuation[1])
        else:
            raise AssertionError("manual continuation did not finish")
        assert len(pages) > 1
        body = "\n".join(pages)
        assert re.findall(r"code-line-(\d+)", body) == [f"{i:03}" for i in range(160)]
        assert "Budgetmatch" not in body, "continuation escaped its requested section"
        assert text.count("```\n") >= 2, "the complete file must preserve code fences"
        client.expect(
            "one file supports capture and ordinary line reads\n",
            # fmt: r
            r=code(r"""
                preview <- capture.output(path <- .console$manual("R-exts#Grouped-B"))
                text <- readLines(path, encoding = "UTF-8")
                stopifnot(
                  basename(path) == "R-exts.md",
                  any(grepl("café λ", preview, fixed = TRUE)),
                  any(grepl("code-line-159", text, fixed = TRUE))
                )
                cat("one file supports capture and ordinary line reads\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_preview_block_boundaries(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, manuals=FIXTURES / "r_manuals_preview") as (
        client,
        root,
    ):
        client.send(r='RShowDoc("R-exts#Paragraphs")')
        pages = []
        for _ in range(4):
            output = normalize_paths(client, root)
            assert len(re.findall(r"Paragraph \d", output)) == output.count(
                "This paragraph ends here."
            ), "a paragraph that fits the next preview must not be split"
            assert len(output.encode("utf-8")) < 8192
            pages.append(output)
            continuation = re.search(r"Continue: (.+)", output)
            if not continuation:
                break
            client.send(r=continuation[1])
        assert len(pages) == 2
        assert re.findall(r"Paragraph \d", "\n".join(pages)) == [
            "Paragraph 1",
            "Paragraph 2",
            "Paragraph 3",
        ]
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_files_and_links(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r=".console$manual()")
        normalize_paths(client, root)
        collection = completed_collection(root)
        manifest = json.loads((collection / "manifest.json").read_text())
        assert manifest["format"] >= 8 and manifest["r_version"].startswith("R version")
        manual = next(m for m in manifest["manuals"] if m["name"] == "R-exts")
        assert (
            manual["anchors"]["Grouped-A"]["line"]
            < manual["anchors"]["Grouped-B"]["line"]
        )
        assert (
            manual["anchors"]["Grouped-B"]["section"]
            == manual["anchors"]["Grouped-B-1"]["section"]
        )
        assert (
            manual["anchors"]["inline-anchor"]["section"]
            == manual["anchors"]["Grouped-B"]["section"]
        )
        files = sorted(collection.glob("R-*.md"))
        assert [p.name for p in files] == ["R-exts.md", "R-lang.md"]
        body = (collection / "R-exts.md").read_text(encoding="utf-8")
        for i in range(80):
            assert body.count(f"paragraph-{i:03}:") == 1, i
        for i in range(160):
            assert body.count(f"code-line-{i:03}") == 1, i
        assert "keep <- function(x) {\n  x + 1L\n}" in body
        assert "| Name | Value |" in body and "| café | 42 |" in body
        assert "Example table" in body
        assert "[(1)](#Grouped-B)" in body
        assert "hidden-script" not in body and "hidden-style" not in body
        assert "unwanted navigation repetition" not in body
        assert "https://example.org/external" in body
        assert "unavailable local target: missing.html#absent" in body
        for file in [collection / "index.md", *files]:
            for destination in re.findall(r"\]\(([^)]+)\)", file.read_text()):
                if ":" in destination:
                    continue
                target, _, anchor = destination.partition("#")
                selected = file.parent / target if target else file
                assert selected.is_file(), (file, destination)
                if anchor:
                    assert f'<a id="{anchor}"></a>' in selected.read_text(), (
                        file,
                        destination,
                    )
        # Ordinary recursive text search must find each body in one document.
        matches = [
            p.name
            for p in collection.rglob("*")
            if p.is_file() and "Parent-only text." in p.read_text(encoding="utf-8")
        ]
        assert matches == ["R-exts.md"], matches
        client.expect(
            "file access and R navigation use the same cache\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts#Grouped-B"))
                text <- readLines(path, encoding = "UTF-8")
                stopifnot(
                  basename(path) == "R-exts.md",
                  any(grepl('<a id="Grouped-B"></a>', text, fixed = TRUE)),
                  any(grepl("café λ", text, fixed = TRUE)),
                  any(grepl("needle", text, ignore.case = TRUE))
                )
                cat("file access and R navigation use the same cache\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_reuse_and_invalidation(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        assert not cache_entries(root), "startup must not convert manuals"
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        first = completed_collection(root)
        timestamps = {
            p.relative_to(first): p.stat().st_mtime_ns
            for p in first.rglob("*")
            if p.is_file()
        }
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        assert completed_collection(root) == first
        assert timestamps == {p: (first / p).stat().st_mtime_ns for p in timestamps}
        client.send(control="restart")
        client.send(r='utils::RShowDoc("R-exts")')
        normalize_paths(client, root)
        assert completed_collection(root) == first
        assert timestamps == {p: (first / p).stat().st_mtime_ns for p in timestamps}
        source = root / "doc/manual/R-exts.html"
        source.write_text(
            source.read_text().replace("Parent-only text.", "Changed source text.")
        )
        client.send(r='.console$manual("R-exts#Small-sections")')
        assert "Changed source text." in normalize_paths(client, root)
        assert len(cache_entries(root)) == 2
        source.write_bytes(b"<html>\xff</html>")
        client.send(r='RShowDoc("R-exts")')
        assert "UTF-8" in normalize_paths(client, root)
        assert len(cache_entries(root)) == 2
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_repairs_modified_outputs(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(
            r='discard <- capture.output(path <- .console$manual("R-exts#Small-sections"))'
        )
        normalize_paths(client, root)
        collection = completed_collection(root)
        original = {
            p.relative_to(collection): p.read_bytes()
            for p in collection.rglob("*")
            if p.is_file()
        }
        client.expect(
            "modified document and manifest\n",
            # fmt: r
            r=code(r"""
                manifest <- file.path(dirname(path), "manifest.json")
                for (file in c(path, manifest)) {
                  text <- readLines(file, encoding = "UTF-8", warn = FALSE)
                  text <- sub("Small sections", "INJECTED-CACHE-TEXT", text, fixed = TRUE)
                  writeLines(
                    sub("Parent-only text.", "INJECTED-CACHE-TEXT", text, fixed = TRUE),
                    file,
                    useBytes = TRUE
                  )
                }
                cat("modified document and manifest\n")
                """),
        )
        client.send(r='.console$manual_search("INJECTED-CACHE-TEXT")')
        assert "No matches." in normalize_paths(client, root)
        assert original == {p: (collection / p).read_bytes() for p in original}
        client.expect("[done]", r="unlink(path)")
        client.send(r='.console$manual("R-exts#Small-sections")')
        assert "Parent-only text." in normalize_paths(client, root)
        assert original == {p: (collection / p).read_bytes() for p in original}
        client.expect("[done]", r='writeLines("invalid manifest", manifest)')
        client.send(control="restart")
        client.send(r='utils::RShowDoc("R-exts")')
        assert "Writing R Extensions" in normalize_paths(client, root)
        assert original == {p: (collection / p).read_bytes() for p in original}
        client.expect(
            "[done]",
            r='discard <- capture.output(path <- .console$manual("R-exts")); writeLines("INJECTED-CACHE-TEXT", file.path(dirname(path), "R-lang.md"))',
        )
        client.send(r='RShowDoc("R-lang#Shared-anchor")')
        normalize_paths(client, root)
        assert original == {p: (collection / p).read_bytes() for p in original}
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_bounded_anchor_continues_current_section(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='.console$manual("R-exts#Grouped-A", lines = 1L)')
        output = normalize_paths(client, root)
        continuation = re.search(r"Continue: (.+)", output)
        assert continuation and "Next section:" not in output, output
        client.send(r=continuation[1])
        output = normalize_paths(client, root)
        assert "1.1 Grouped A" in output and "A short needle introduction." in output, (
            output
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_rejects_unprojected_files(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(
            r='discard <- capture.output(path <- .console$manual("R-exts#Small-sections"))'
        )
        normalize_paths(client, root)
        collection = completed_collection(root)
        original = {
            p.relative_to(collection): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in collection.rglob("*")
            if p.is_file()
        }
        client.send(control="restart")
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        assert original == {
            p: ((collection / p).read_bytes(), (collection / p).stat().st_mtime_ns)
            for p in original
        }, "valid documents must not invalidate a completed collection"
        client.expect(
            "added unprojected files\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts#Small-sections"))
                collection <- dirname(path)
                for (name in c(
                  "unexpected.md",
                  "unexpected/injected.md",
                  "nested/deeper/injected.md"
                )) {
                  file <- file.path(collection, name)
                  dir.create(dirname(file), recursive = TRUE, showWarnings = FALSE)
                  writeLines("INJECTED-CACHE-TEXT", file)
                  Sys.chmod(file, mode = "444")
                }
                cat("added unprojected files\n")
                """),
        )
        client.send(control="restart")
        client.send(r='utils::RShowDoc("R-exts#Small-sections")')
        assert "Parent-only text." in normalize_paths(client, root)
        files = {
            p.relative_to(collection) for p in collection.rglob("*") if p.is_file()
        }
        assert files == set(original), files
        assert not (collection / "nested").exists()
        assert not any(
            "INJECTED-CACHE-TEXT" in (collection / p).read_text(encoding="utf-8")
            for p in files
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_table_rows_and_unanchored_headings(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, manuals=FIXTURES / "r_manuals_anchors") as (
        client,
        root,
    ):
        client.send(
            r='.console$manual_search("Unanchored reference", manual = "R-exts")'
        )
        output = normalize_paths(client, root)
        command = re.search(r"  Read: (.+)", output)[1]
        assert command != 'RShowDoc("R-exts#")', command
        client.send(r=command)
        output = normalize_paths(client, root)
        assert "Unanchored reference" in output and "Reference body." in output, output
        client.send(r='utils::RShowDoc("R-exts#letter-R")')
        output = normalize_paths(client, root)
        assert "R-only cell" in output and "A-only cell" not in output, output
        assert "<a id=" not in output, output
        client.send(r='RShowDoc("R-exts#child")')
        normalize_paths(client, root)
        collection = completed_collection(root)
        body = (collection / "R-exts.md").read_text(encoding="utf-8")
        row = next(line for line in body.splitlines() if "R-only cell" in line)
        assert '<a id="letter-R"></a>' in row, row
        manifest = json.loads((collection / "manifest.json").read_text())
        sections = manifest["manuals"][0]["sections"]
        child = next(section for section in sections if "Child" in section["title"])
        assert sections[child["parent"]]["title"] == "2 Unanchored reference"
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_repairs_file_identity(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        client.expect(
            "identity replaced by a file\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts"))
                collection <- dirname(path)
                unlink(collection, recursive = TRUE)
                writeLines("invalid identity", collection)
                cat("identity replaced by a file\n")
                """),
        )
        client.send(r='RShowDoc("R-exts#Grouped-B")')
        assert "café λ" in normalize_paths(client, root)
        assert completed_collection(root).is_dir()
        return client.finish()


@requires(R)
@executions(DIRECT)
def test_manual_cache_repairs_directory_links(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        target = root / "workspace/unrelated-directory"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("unrelated content", encoding="utf-8")
        client.send(r='discard <- capture.output(path <- .console$manual("R-exts"))')
        normalize_paths(client, root)
        collection = completed_collection(root)
        staging = collection.with_name(f".{collection.name}.incomplete")
        manuals = collection.parent
        for entry in (collection, staging, manuals):
            if entry.exists():
                shutil.rmtree(entry)
            if os.name == "nt":
                # Junctions need no developer mode or symlink privilege.
                subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(entry), str(target)],
                    check=True,
                    capture_output=True,
                )
            else:
                entry.symlink_to(target, target_is_directory=True)
            if entry == manuals:
                result = client.send(control="restart")
                assert not result.get("isError", False), result
                client.expect("[1] 2\n", r="1 + 1")
            client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
            output = normalize_paths(client, root)
            assert "café λ" in output, output
            assert sentinel.read_text(encoding="utf-8") == "unrelated content"
            assert set(target.iterdir()) == {sentinel}, (
                "repair must not traverse the link"
            )
            completed_collection(root)
        return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_serializes_shared_repairs(
    binary: Path, execution: Execution
) -> Transcript:
    import fcntl

    with manual_client(binary, execution) as (first, root):
        environment, _ = r_test_environment()
        environment = bare_runtime_environment(environment, root / "library")
        environment.update(
            MCP_CONSOLE_LANGUAGES="r",
            XDG_CACHE_HOME=str(root / "cache"),
            CONSOLE_TEST_DOC=str(root / "doc"),
            R_PROFILE_USER=str(root / "workspace/.Rprofile"),
        )
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                *(
                    ("--writable-root", str(root / "workspace"))
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            environment,
            current_directory=root / "workspace",
            use_r_startup_files=True,
        ) as peer:
            peer.initialize_and_list_tools()
            for client in (first, peer):
                client.send(r='RShowDoc("R-exts")')
                assert "Writing R Extensions" in normalize_paths(client, root)
            collection = completed_collection(root)
            # A peer can hold an advisory lock indefinitely. Both prepared
            # workers must report contention without entering cache repair.
            with collection.parent.with_suffix(".lock").open("rb") as gate:
                fcntl.flock(gate, fcntl.LOCK_EX)
                (collection / "index.md").write_text("modified collection")
                for client in (first, peer):
                    client.send(r='RShowDoc("R-exts")')
                    output = normalize_paths(client, root)
                    assert (
                        "Manual cache is busy; retry the manual operation" in output
                    ), output
                    assert "Writing R Extensions" not in output, output
                assert (collection / "index.md").read_text() == "modified collection"
                fcntl.flock(gate, fcntl.LOCK_UN)
            for client in (first, peer):
                client.send(r='RShowDoc("R-exts")')
                assert "Writing R Extensions" in normalize_paths(client, root)
            assert "modified collection" not in (collection / "index.md").read_text()
            assert completed_collection(root) == collection
            peer.finish()
            first.finish()
    return [
        {
            "shared_repairs_serialized": True,
            "lock_contention_returns_retry_error": True,
            "completed_collection_reused": True,
        }
    ]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_section_preview(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution, manuals=FIXTURES / "r_manuals_reading") as (
        client,
        root,
    ):
        client.send(r='RShowDoc("R-exts#Export")')
        output = normalize_paths(client, root)
        assert "Semicolon separators and decimal commas" in output, output
        assert "A later example" in output, output
        assert "Unrelated next-topic text" not in output, output
        collection = completed_collection(root)
        body = (collection / "R-exts.md").read_text(encoding="utf-8")
        assert "Earlier background material" in body and "A later example" in body
        assert "Earlier background material" not in output, output
        assert "File: R-exts.md" in output, output
        client.send(r='.console$manual_search("Export guidance", manual = "R-exts")')
        output = normalize_paths(client, root)
        assert "Choose the output format" in output, output
        client.send(
            r='.console$manual_search("Semicolon separators", manual = "R-exts")'
        )
        output = normalize_paths(client, root)
        assert 'Read: RShowDoc("R-exts#Export")' in output, output
        client.send(r='.console$manual_search("This introduction", manual = "R-exts")')
        output = normalize_paths(client, root)
        assert "1.2 Export guidance" in output, output
        assert "This introduction" in output, output
        command = re.search(r"  Read: (.+)", output)[1]
        client.send(r=command)
        assert re.search(r"This\s+introduction", normalize_paths(client, root))
        return client.finish()


@requires(UNPRIVILEGED, R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_repairs_directory_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        collection = completed_collection(root)
        for target in (
            "dirname(collection)",
            "collection",
            'file.path(collection, "unexpected")',
        ):
            try:
                client.expect(
                    "directory permissions removed\n",
                    # fmt: r
                    r=code(r"""
                        discard <- capture.output(path <- .console$manual("R-exts"))
                        collection <- dirname(path)
                        directory <- DIRECTORY
                        if (!dir.exists(directory)) {
                          dir.create(directory)
                        }
                        stopifnot(Sys.chmod(directory, mode = "000"))
                        cat("directory permissions removed\n")
                        """).replace("DIRECTORY", target),
                )
                normalize_paths(client, root)
                if target == "dirname(collection)":
                    client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
                    output = normalize_paths(client, root)
                    assert "café λ" in output, output
                    client.expect(
                        "root permissions removed again before restart\n",
                        # fmt: r
                        r=code(r"""
                            stopifnot(Sys.chmod(dirname(collection), mode = "000"))
                            cat("root permissions removed again before restart\n")
                            """),
                    )
                client.send(control="restart")
                client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
                output = normalize_paths(client, root)
                assert "café λ" in output, output
                assert completed_collection(root) == collection
            finally:
                # Keep failure cleanup possible when exercising the old worker.
                collection.parent.chmod(0o700)
                collection.chmod(0o700)
                if (collection / "unexpected").exists():
                    (collection / "unexpected").chmod(0o700)
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_navigation_with_manual_like_directory(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, doc_component="R-old.html#archive") as (
        client,
        root,
    ):
        client.send(r='RShowDoc("R-exts")')
        output = normalize_paths(client, root)
        assert "Writing R Extensions" in output, output
        client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
        output = normalize_paths(client, root)
        assert "café λ" in output, output
        client.expect(
            "capture and native return retained\n",
            # fmt: r
            r=code(r"""
                output <- capture.output(
                  value <- withVisible(utils::RShowDoc(
                    "R-exts#Grouped-B"
                  ))
                )
                stopifnot(
                  any(grepl("café λ", output, fixed = TRUE)),
                  !value$visible,
                  identical(value$value, file.path(R.home("doc"), "manual", "R-exts.html"))
                )
                cat("capture and native return retained\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(SANDBOXED)
def test_manual_cache_lock_is_host_owned(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        collection = completed_collection(root)
        lock = collection.parent.with_suffix(".lock")
        assert lock.is_file(), "publication lock must live outside the writable subtree"
        client.expect(
            "host lock cannot be replaced\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts"))
                lock <- paste0(dirname(dirname(path)), ".lock")
                stopifnot(unlink(lock) != 0L, file.exists(lock))
                cat("host lock cannot be replaced\n")
                """),
        )
        client.send(control="restart")
        client.send(r='RShowDoc("R-exts#Grouped-B")')
        assert "café λ" in normalize_paths(client, root)
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_loss_does_not_block_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.expect("[1] 2\n", r="1 + 1")
        cache = root / "cache/mcp-console"
        manuals = cache / "manuals"
        for removed in (manuals, cache):
            client.send(r="old_session <- TRUE")
            shutil.rmtree(removed)
            result = client.send(control="restart")
            assert not result.get("isError", False), result
            client.expect(
                "ordinary R execution works after cache loss\n",
                # fmt: r
                r=code(r"""
                    stopifnot(!exists("old_session"), 1 + 1 == 2)
                    cat("ordinary R execution works after cache loss\n")
                    """),
            )
            assert manuals.is_dir(), "restart must recreate the cache root"
            assert manuals.with_suffix(".lock").is_file()
            assert not cache_entries(root), "materialization must remain lazy"
            client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
            assert "café λ" in normalize_paths(client, root)
            completed_collection(root)
        return client.finish()


@requires(R)
@executions(DIRECT)
def test_manual_cache_file_does_not_block_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='discard <- capture.output(path <- .console$manual("R-exts"))')
        normalize_paths(client, root)
        client.send(
            # fmt: r
            r=code(r"""
                old_session <- TRUE
                root <- dirname(dirname(path))
                unlink(root, recursive = TRUE)
                writeLines("replaced by submitted code", root)
                """),
        )
        result = client.send(control="restart")
        assert not result.get("isError", False), result
        client.expect("[1] 2\n", r='stopifnot(!exists("old_session")); 1 + 1')
        assert (root / "cache/mcp-console/manuals").is_dir()
        assert not cache_entries(root), "recovery must keep conversion lazy"
        client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
        assert "café λ" in normalize_paths(client, root)
        return client.finish()


@requires(R)
@executions(DIRECT)
def test_manual_cache_lock_directory_does_not_block_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        lock = root / "cache/mcp-console/manuals.lock"
        inode = lock.stat().st_ino
        client.send(control="restart")
        assert lock.stat().st_ino == inode, "keep an existing publisher's lock inode"
        client.send(
            # fmt: r
            r=code(r"""
                old_session <- TRUE
                lock <- paste0(Sys.getenv("MCP_CONSOLE_MANUAL_CACHE"), ".lock")
                unlink(lock)
                stopifnot(dir.create(lock))
                dir.create(file.path(lock, "unexpected"))
                writeLines("invalid lock", file.path(lock, "unexpected", "entry"))
                stopifnot(Sys.chmod(file.path(lock, "unexpected"), mode = "000"))
                """),
        )
        result = client.send(control="restart")
        assert not result.get("isError", False), result
        client.expect("[1] 2\n", r='stopifnot(!exists("old_session")); 1 + 1')
        assert lock.is_file(), "restart must replace the invalid lock"
        assert not cache_entries(root), "recovery must keep conversion lazy"
        client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
        assert "café λ" in normalize_paths(client, root)
        return client.finish()


@requires(POSIX, R, command("cc"))
@executions(DIRECT, SANDBOXED)
def test_manual_cache_directory_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, cache_race="manual_cache_directory") as (
        client,
        root,
    ):
        target = root / "workspace/unrelated-directory"
        target.mkdir()
        sentinel = target / "keep.txt"
        sentinel.write_text("unrelated content", encoding="utf-8")
        client.send(r='discard <- capture.output(path <- .console$manual("R-exts"))')
        normalize_paths(client, root)
        collection = completed_collection(root)
        for directory in (collection, collection / "unexpected"):
            directory.mkdir(exist_ok=True)
            (collection / "R-exts.md").write_text("invalid cache")
            marker = root / "workspace/directory-replaced"
            marker.unlink(missing_ok=True)
            client.send(
                # fmt: r
                r=code(r"""
                    Sys.setenv(
                      CONSOLE_TEST_CACHE_DIRECTORY = if (NESTED) {
                        file.path(dirname(path), "unexpected")
                      } else {
                        dirname(path)
                      },
                      CONSOLE_TEST_CACHE_DIRECTORY_SKIP = if (NESTED) "0" else "1",
                      CONSOLE_TEST_CACHE_LINK_TARGET = file.path(getwd(), "unrelated-directory"),
                      CONSOLE_TEST_CACHE_DIRECTORY_MARKER = file.path(getwd(), "directory-replaced")
                    )
                    """).replace(
                    "NESTED", "TRUE" if directory != collection else "FALSE"
                ),
            )
            client.send(r='discard <- capture.output(RShowDoc("R-exts"))')
            assert marker.exists(), "replace the directory at its enumeration boundary"
            assert sentinel.exists(), "repair must not delete the symlink target"
            assert sentinel.read_text(encoding="utf-8") == "unrelated content"
            assert set(target.iterdir()) == {sentinel}
            # Concurrent replacement may interrupt repair; an explicit retry
            # must recover without traversing the link or losing source text.
            client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
            assert "café λ" in normalize_paths(client, root)
            completed_collection(root)
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_reclaims_abandoned_publication(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        collection = completed_collection(root)
        staging = collection.with_name(f".{collection.name}.incomplete")
        shutil.copytree(collection, staging)
        client.send(control="restart")
        client.send(r='RShowDoc("R-exts")')
        assert "Writing R Extensions" in normalize_paths(client, root)
        assert not staging.exists(), (
            "retry must reclaim abandoned staging even on reuse"
        )
        # Repeat with the final entry missing, as after interrupted first use.
        shutil.copytree(collection, staging)
        shutil.rmtree(collection)
        client.send(control="restart")
        client.send(r='RShowDoc("R-exts")')
        assert "Writing R Extensions" in normalize_paths(client, root)
        assert not staging.exists()
        assert completed_collection(root) == collection
        return client.finish()


@requires(POSIX, R, command("cc"))
@executions(DIRECT, SANDBOXED)
def test_manual_cache_fifo_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, cache_race="manual_cache_fifo") as (
        client,
        root,
    ):
        client.send(r='discard <- capture.output(path <- .console$manual("R-exts"))')
        normalize_paths(client, root)
        path = completed_collection(root) / "R-exts.md"
        marker = root / "workspace/cache-file-replaced"
        client.send(
            # fmt: r
            r=code(r"""
                Sys.setenv(
                  CONSOLE_TEST_CACHE_OPEN_TARGET = path,
                  CONSOLE_TEST_CACHE_OPEN_MARKER = file.path(getwd(), "cache-file-replaced")
                )
                """),
        )
        try:
            pending = client.start_send(
                r='RShowDoc("R-exts#Grouped-B")', timeout_ms=10000
            )
            wait_for_path(marker, "cache entry replaced at open", client=client)
            client.receive(pending)
            assert "café λ" in normalize_paths(client, root), (
                "a FIFO replacement must be repaired without waiting for a writer"
            )
            assert path.is_file()
        finally:
            # Release a blocking old implementation after the public timeout;
            # the successful implementation repairs the FIFO before responding.
            if path.is_fifo():
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                os.close(descriptor)
        return client.finish()


@requires(POSIX, R, command("cc"))
@executions(DIRECT, SANDBOXED)
def test_manual_publication_fifo_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution, cache_race="manual_cache_fifo") as (
        client,
        root,
    ):
        client.send(r='discard <- capture.output(path <- .console$manual("R-exts"))')
        normalize_paths(client, root)
        collection = completed_collection(root)
        staging = collection.with_name(f".{collection.name}.incomplete")
        path = staging / "R-exts.md"
        marker = root / "workspace/cache-file-replaced"
        client.send(
            # fmt: r
            r=code(r"""
                Sys.setenv(
                  CONSOLE_TEST_CACHE_OPEN_TARGET = file.path(
                    dirname(dirname(path)),
                    paste0(".", basename(dirname(path)), ".incomplete"),
                    "R-exts.md"
                  ),
                  CONSOLE_TEST_CACHE_OPEN_MARKER = file.path(getwd(), "cache-file-replaced")
                )
                """),
        )
        (collection / "R-exts.md").write_text("invalid cache")
        try:
            pending = client.start_send(r='RShowDoc("R-exts")', timeout_ms=10000)
            wait_for_path(marker, "publication file replaced at open", client=client)
            client.receive(pending)
            output = normalize_paths(client, root)
            assert (
                "Manual publication interrupted; retry the manual operation" in output
            ), output
            assert not (collection / "R-exts.md").read_text().startswith("<!--")
        finally:
            if path.is_fifo():
                descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
                os.close(descriptor)
        client.send(r='utils::RShowDoc("R-exts#Grouped-B")')
        assert "café λ" in normalize_paths(client, root)
        assert not staging.exists()
        completed_collection(root)
        return client.finish()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_manual_cache_repairs_sparse_files(
    binary: Path, execution: Execution
) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='cat(Sys.getpid(), "\\n")')
        pid = int(normalize_paths(client, root).strip())
        client.send(r='RShowDoc("R-exts")')
        normalize_paths(client, root)
        collection = completed_collection(root)
        paths = (
            collection / "manifest.json",
            collection / "R-exts.md",
            collection / "R-lang.md",
        )
        original = [path.read_bytes() for path in paths]
        # Linux direct execution can cap the worker's address space for a safe
        # red run: an unbounded 16 GiB read fails without exhausting the host.
        limits = None
        if sys.platform == "linux" and execution == DIRECT:
            import resource

            status = Path(f"/proc/{pid}/status").read_text()
            virtual = int(re.search(r"VmSize:\s+(\d+)", status)[1]) * 1024
            limits = resource.prlimit(pid, resource.RLIMIT_AS)
            resource.prlimit(
                pid, resource.RLIMIT_AS, (virtual + 128 * 1024**2, limits[1])
            )
        try:
            for path, expected in zip(paths, original, strict=True):
                with path.open("wb") as file:
                    file.truncate(16 * 1024**3)
                client.send(r='RShowDoc("R-exts")')
                normalize_paths(client, root)
                assert path.read_bytes() == expected
        finally:
            if limits is not None:
                try:
                    resource.prlimit(pid, resource.RLIMIT_AS, limits)
                except ProcessLookupError:
                    pass
        return [{"oversized_manifest_and_documents_repaired": True}]
