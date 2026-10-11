"""Installed manuals share a persistent, navigable Markdown representation."""

import json
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import FIXTURES, r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.resolvers import bare_runtime_environment


@contextmanager
def manual_client(
    binary: Path, execution: Execution, *, installed: bool = False
) -> Iterator[tuple[McpClient, Path]]:
    with tempfile.TemporaryDirectory(prefix="console-manuals-café#-") as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        environment, _ = r_test_environment()
        library = root / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        environment.update(
            MCP_CONSOLE_LANGUAGES="r", XDG_CACHE_HOME=str(root / "cache")
        )
        doc = root / "doc"
        if not installed:
            shutil.copytree(FIXTURES / "r_manuals", doc / "manual")
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


def normalize_paths(client: McpClient, root: Path) -> str:
    output = last_tool_text(client)
    assert "[output omitted:" not in output, output
    text = output.replace(str(root), "<fixture>").replace("\\", "/")
    # The identity includes the running R version and the native source path.
    text = re.sub(r"/manuals/[a-f0-9]{64}", "/manuals/<identity>", text)
    text = re.sub(r"(?m)^R version \d[^\n]*$", "R version <installed>", text)
    client.transcript[-1]["result"]["content"][0]["text"] = text
    return output


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_navigation(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r='RShowDoc("R-exts")')
        output = normalize_paths(client, root)
        assert "Writing R Extensions" in output, output
        assert "1 Small sections" in output, output
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
                a <- capture.output(.console$manual(
                  "R-exts#Oversized-example",
                  start = 15L,
                  lines = 4L
                ))
                stopifnot(length(a) < 12L, any(grepl("code-line-", a, fixed = TRUE)))
                b <- capture.output(.console$manual_search("absent search phrase"))
                stopifnot(any(grepl("No matches", b, fixed = TRUE)))
                cat("bounded reading and capture retained\n")
                """),
        )
        normalize_paths(client, root)
        for query in ("code-line-159", "wide-end"):
            client.send(r=f'.console$manual_search("{query}")')
            output = normalize_paths(client, root)
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


def completed_collection(root: Path) -> Path:
    cache = root / "cache/mcp-console/manuals"
    entries = sorted(cache.iterdir())
    assert entries and all(re.fullmatch("[a-f0-9]{64}", p.name) for p in entries), (
        entries
    )
    assert all((p / "manifest.json").is_file() for p in entries), entries
    return entries[-1]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_manual_files_and_export(binary: Path, execution: Execution) -> Transcript:
    with manual_client(binary, execution) as (client, root):
        client.send(r=".console$manual()")
        normalize_paths(client, root)
        collection = completed_collection(root)
        manifest = json.loads((collection / "manifest.json").read_text())
        assert manifest["format"] >= 1 and manifest["r_version"].startswith("R version")
        manual = next(m for m in manifest["manuals"] if m["name"] == "R-exts")
        assert manual["anchors"]["Grouped-A"] == manual["anchors"]["Grouped-B"]
        assert manual["anchors"]["Grouped-B"] == manual["anchors"]["Grouped-B-1"]
        assert manual["anchors"]["inline-anchor"] == manual["anchors"]["Grouped-B"]
        assert manual["anchors"]["Appendix-B"].startswith("B-fixture-reference-")
        files = sorted((collection / "R-exts").glob("*.md"))
        bodies = "\n".join(p.read_text() for p in files if p.name != "index.md")
        assert len(files) > 5, files
        for i in range(80):
            assert bodies.count(f"paragraph-{i:03}:") == 1, i
        for i in range(160):
            assert bodies.count(f"code-line-{i:03}") == 1, i
        assert "keep <- function(x) {\n  x + 1L\n}" in bodies
        assert "| Name | Value |" in bodies and "| café | 42 |" in bodies
        assert "Example table" in bodies
        assert "[(1)](01-small-sections-part-001.md#Grouped-B)" in bodies
        assert "hidden-script" not in bodies and "hidden-style" not in bodies
        assert "unwanted navigation repetition" not in bodies
        assert "https://example.org/external" in bodies
        assert "unavailable local target: missing.html#absent" in bodies
        for file in files:
            for destination in re.findall(r"\]\(([^)]+)\)", file.read_text()):
                if ":" in destination:
                    continue
                target, _, anchor = destination.partition("#")
                selected = file.parent / target
                assert selected.is_file(), (file, destination)
                if anchor:
                    assert f'<a id="{anchor}"></a>' in selected.read_text(), (
                        file,
                        destination,
                    )
        coherent = collection / "R-exts" / manual["anchors"]["Oversized-example"]
        code_files = [p for p in files if "code-line-000" in p.read_text()]
        assert len(code_files) == 1 and "code-line-159" in code_files[0].read_text()
        assert code_files[0].stat().st_size > 6144
        assert coherent.is_file()
        client.send(r='.console$manual_export("R-exts")')
        normalize_paths(client, root)
        complete = (collection / "exports/R-exts.md").read_text()
        assert "](#Grouped-B)" in complete and "](../R-lang/" in complete
        assert "[(1)](#Grouped-B)" in complete
        assert all(complete.count(f"paragraph-{i:03}:") == 1 for i in range(80))
        client.expect(
            "file access and R navigation use the same cache\n",
            # fmt: r
            r=code(r"""
                discard <- capture.output(path <- .console$manual("R-exts#Grouped-B"))
                text <- readLines(path, encoding = "UTF-8")
                stopifnot(
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
        cache = root / "cache/mcp-console/manuals"
        assert not list(cache.iterdir()), "startup must not convert manuals"
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
        assert len(list(cache.iterdir())) == 2
        source.write_bytes(b"<html>\xff</html>")
        client.send(r='RShowDoc("R-exts")')
        assert "UTF-8" in normalize_paths(client, root)
        assert len(list(cache.iterdir())) == 2
        assert all((p / "manifest.json").is_file() for p in cache.iterdir())
        return client.finish()
