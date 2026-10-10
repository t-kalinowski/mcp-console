#!/usr/bin/env -S uv run --script

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import (
    NATIVE_EURO_LOCALE,
    POSIX,
    R,
    native_euro_locale,
    r_french_messages,
    requires,
)
from support.suites import run_this_suite

GUIDE = Path(__file__).resolve().parents[4] / "docs/R_HELP.md"


def recipe(name: str) -> str:
    match = re.search(
        rf"<!-- recipe: {name} -->\s*```r\n(.*?)\n```",
        GUIDE.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, f"missing R help recipe: {name}"
    return match[1] + "\n"


def help_output(client: McpClient, package: str, topic: str) -> str:
    text = last_tool_text(client)
    assert len(text.encode("utf-8")) < 8192, text
    assert text.startswith(f"{package}::{topic} | package "), text
    assert "[running" not in text and "[input" not in text, text
    header, library, line_range, _ = text.split("\n", 3)
    assert re.fullmatch(
        rf"{re.escape(package)}::{re.escape(topic)} \| package \d+\.\d+\.\d+ \| R \d+\.\d+\.\d+",
        header,
    ), text
    assert Path(library.removeprefix("Library: ")).is_absolute(), text
    assert (Path(library.removeprefix("Library: ")) / package).is_dir(), text
    window = re.fullmatch(r"Lines (\d+)-(\d+) of (\d+)", line_range)
    assert window is not None, text
    first, last, total = map(int, window.groups())
    assert 1 <= first <= last <= total, text
    assert text.endswith(
        f"Next line: {last + 1}\n" if last < total else "End of help.\n"
    ), text
    normalized = text.replace(
        header, f"{package}::{topic} | package <version> | R <version>", 1
    )
    normalized = normalized.replace(library, "Library: <selected installed library>", 1)
    normalized = normalized.replace(line_range, "Lines <displayed range> of <total>", 1)
    if last < total:
        normalized = normalized.removesuffix(f"Next line: {last + 1}\n")
        normalized += "Next line: <continuation>\n"
    client.transcript[-1]["result"]["content"][0]["text"] = normalized
    return text


def rendered_mean_help(environment: dict[str, str], rscript: Path) -> str:
    # Compare the whole page with the selected installation, not one R manual.
    # fmt: r
    reference_source = code(r"""
        options(
          useFancyQuotes = FALSE,
          pager = function(files, header, title, delete.file) {
            writeLines(enc2utf8(readLines(files, warn = FALSE)), useBytes = TRUE)
            if (delete.file) unlink(files)
          }
        )
        tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
        print(utils::help("mean", package = "base", help_type = "text"))
        """)
    return subprocess.check_output(
        [rscript, "--vanilla", "-"],
        input=reference_source,
        env=environment,
        text=True,
        encoding="utf-8",
    )


# Native Windows has a GUI default pager; the shared case uses the scoped pager.
@requires(R, POSIX)
@executions(RUNTIME)
def test_reads_installed_topic_with_default_pager(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    reference = rendered_mean_help(environment, rscript)
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        # Fix presentation across R versions; keep Console's default pager.
        client.expect(
            # fmt: r
            r=code(r"""
                options(useFancyQuotes = FALSE)
                tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
                """),
        )
        client.send(r=recipe("ordinary-topic"))
        text = last_tool_text(client)
        assert len(text.encode("utf-8")) < 8192, text
        assert "package:base" in text and "Arithmetic Mean" in text, text
        assert "Examples:" in text, text
        assert "[running" not in text and "[input" not in text, text
        # R's default file.show pager adds a newline after each rendered file.
        assert text == reference + "\n", repr(text)
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "Installed base::mean text matches Rscript reference.\n"
        )
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_reads_installed_topics_and_discovers_candidates(
    binary: Path, execution: Execution
) -> Transcript:
    quick_topic = recipe("quick-topic")
    definitions = recipe("definitions")
    environment, rscript = r_test_environment()
    reference = rendered_mean_help(environment, rscript)
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        # Cover the native default (including Windows' GUI pager), then a
        # custom viewer that must not be invoked by explicit retrieval.
        for setup in (
            # fmt: r
            code(r"""
                options(useFancyQuotes = FALSE)
                tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
                inherited_options <- options()[c("help_type", "browser", "pager")]
                inherited_format <- tools::Rd2txt_options()
                """),
            # fmt: r
            code(r"""
                options(
                  help_type = "html",
                  browser = function(...) stop("browser invoked"),
                  pager = function(...) stop("inherited pager invoked"),
                  menu.graphics = TRUE,
                  useFancyQuotes = FALSE
                )
                tools::Rd2txt_options(width = 80L, underline_titles = FALSE)
                inherited_options <- options()[c("help_type", "browser", "pager")]
                inherited_format <- tools::Rd2txt_options()
                """),
        ):
            client.expect(r=setup)
            client.send(r=recipe("text-pager"))
            text = last_tool_text(client)
            assert len(text.encode("utf-8")) < 8192, text
            assert "package:base" in text and "Arithmetic Mean" in text, text
            assert "Examples:" in text, text
            assert "[running" not in text and "[input" not in text, text
            assert text == reference, repr(text)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "Installed base::mean text matches Rscript reference.\n"
            )
            client.expect(
                r="stopifnot(identical(options()[names(inherited_options)], inherited_options))",
            )
        client.send(r=quick_topic)
        text = last_tool_text(client)
        assert len(text.encode("utf-8")) < 8192, text
        assert "Fitting Linear Models" in text, text
        assert text.endswith("[Excerpt; full text retained in help_text.]\n"), text
        client.send(r=recipe("quick-version"))
        assert re.fullmatch(r"\[1\] [‘']\d+\.\d+\.\d+[’']\n", last_tool_text(client))
        client.transcript[-1]["result"]["content"][0]["text"] = "[1] '<version>'\n"
        client.send(
            r=quick_topic.replace(
                'topic <- "lm"', 'topic <- "mcpconsolemissinghelptopic"'
            )
        )
        assert last_tool_text(client) == "Error: Expected one installed help page\n"
        client.expect(
            # fmt: r
            r=code(r"""
                stopifnot(
                  identical(options()[names(inherited_options)], inherited_options),
                  identical(tools::Rd2txt_options(), inherited_format),
                  any(grepl("Fitting Linear Models", help_text, fixed = TRUE)),
                  sum(nchar(help_text, type = "bytes") + 1L) > 6000L
                )
                writeLines(
                  "Quick lookup retains long text and restores settings after success and failure"
                )
                """),
            expected="Quick lookup retains long text and restores settings after success and failure\n",
        )
        client.expect(r=definitions)
        client.send(r=recipe("qualified"))
        assert "Fitting Linear Models" in help_output(client, "stats", "lm")
        client.send(r=recipe("operator"))
        assert "Extract or Replace Parts of an Object" in help_output(
            client, "base", "["
        )
        client.send(r=recipe("candidates"))
        text = last_tool_text(client)
        assert text == (
            "base::plot | Generic X-Y Plotting\n"
            "graphics::plot.default | The Default Scatterplot Function\n"
        ), text
        client.expect(
            r=recipe("candidates").replace(
                '"^plot$"', '"^mcpconsolemissinghelptopic$"'
            ),
        )
        client.send(r=recipe("selected"))
        assert "Generic X-Y Plotting" in help_output(client, "base", "plot")
        client.send(r=recipe("index"))
        assert "Index:\n" in help_output(client, "stats", "<index>")
        client.expect(
            # fmt: r
            r=code(r"""
                stopifnot(
                  identical(options()[names(inherited_options)], inherited_options),
                  identical(tools::Rd2txt_options(), inherited_format),
                  identical(lm_page$package, "stats"),
                  identical(lm_page$topic, "lm"),
                  identical(
                    lm_page$version,
                    as.character(
                      utils::packageVersion("stats", lib.loc = lm_page$library)
                    )
                  ),
                  identical(lm_page$r_version, as.character(getRversion())),
                  identical(
                    file.path(lm_page$library, "stats"),
                    find.package("stats", lib.loc = .libPaths())
                  ),
                  any(grepl("Fitting Linear Models", index_page$text, fixed = TRUE))
                )
                writeLines("Installed provenance and inherited settings preserved")
                """),
            expected="Installed provenance and inherited settings preserved\n",
        )
        return client.finish()


@requires(R, r_french_messages())
@executions(RUNTIME)
def test_reads_translated_package_index(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["LANGUAGE"] = "fr"
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: r
            r=code(r"""
                stopifnot(identical(gettext("Index:", domain = "R-base"), "Index :"))
                options(
                  help_type = "html",
                  browser = function(...) stop("browser invoked"),
                  pager = function(...) stop("inherited pager invoked")
                )
                """),
        )
        client.expect(r=recipe("definitions"))
        client.send(r=recipe("index"))
        text = help_output(client, "stats", "<index>")
        assert text.split("\n", 3)[3].startswith("Index :\n\n"), text
        assert "acf" in text, text
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_bounds_pages_and_reports_missing_help_without_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    definitions = recipe("definitions")
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(r=definitions)
        client.expect(
            # fmt: r
            r=code(r"""
                options(useFancyQuotes = FALSE)
                set.seed(72)
                seed_before <- .Random.seed
                pager_before <- getOption("pager")
                format_before <- tools::Rd2txt_options()
                namespaces_before <- loadedNamespaces()
                """),
        )
        declaration = client.send(requirements={"action": "get"})["structuredContent"]
        client.send(
            r=recipe("quick-topic").replace(
                'package <- "stats"', 'package <- "mcpconsolemissinghelppackage"'
            )
        )
        assert last_tool_text(client) == (
            "Error: Package is not installed: mcpconsolemissinghelppackage\n"
        ), last_tool_text(client)
        client.send(r=recipe("missing-package"))
        assert last_tool_text(client) == (
            "Error: Package is not installed: mcpconsolemissinghelppackage\n"
        ), last_tool_text(client)
        assert (
            client.send(requirements={"action": "get"})["structuredContent"]
            == declaration
        )
        client.send(r=recipe("missing-topic"))
        assert last_tool_text(client) == (
            "Error: Expected one help page for stats::mcpconsolemissinghelptopic; found 0\n"
        ), last_tool_text(client)
        client.send(r=recipe("long-page"))
        text = help_output(client, "base", "Quotes")
        assert "Next line: " in text, text
        body = text.split("\n", 3)[3].rsplit("Next line: ", 1)[0]
        assert len(body.encode("utf-8")) <= 240, text
        client.send(r=recipe("later-page"))
        later = help_output(client, "base", "Quotes")
        assert later != text, later
        # Explicit UTF-8 makes the paging fixture independent of R's help locale.
        # A cut inside the second quote must preserve the complete first quote.
        client.send(
            # fmt: r
            r=code(r"""
                quotes_page <- read_r_help("base", "Quotes")
                prefix <- "Unicode paging fixture: "
                quotes_page$text[1L] <- paste0(
                  prefix,
                  "\u2018\u2019",
                  strrep("x", 80L)
                )
                unicode_budget <- nchar(prefix, type = "bytes") + 5L
                show_r_help(quotes_page, from = 1L, n = 1L, bytes = unicode_budget)
                """),
        )
        unicode = help_output(client, "base", "Quotes")
        body = unicode.split("\n", 3)[3].split("Line shortened;", 1)[0]
        assert (
            len(body.encode("utf-8")) <= len("Unicode paging fixture: ".encode()) + 5
        ), unicode
        assert body.endswith("\u2018\n"), unicode
        assert "Line shortened; the full line remains in page$text.\n" in unicode
        client.expect(
            # fmt: r
            r=code(r"""
                stopifnot(
                  length(find.package("mcpconsolemissinghelppackage", quiet = TRUE)) == 0L,
                  identical(getOption("pager"), pager_before),
                  identical(tools::Rd2txt_options(), format_before),
                  identical(loadedNamespaces(), namespaces_before)
                )
                workspace_before <- NULL
                workspace_before <- ls(globalenv(), all.names = TRUE)
                local({
                  normal_page <- read_r_help("stats", "rnorm")
                  stopifnot(any(grepl("rnorm", normal_page$text, fixed = TRUE)))
                  long_page <- read_r_help("stats", "lm")
                  display <- capture.output(show_r_help(long_page, n = length(long_page$text)))
                  stopifnot(
                    sum(nchar(display, type = "bytes") + 1L) < 8192L,
                    grepl("^Next line: ", tail(display, 1L)),
                    sum(nchar(long_page$text, type = "bytes") + 1L) > 6000L
                  )
                })
                stopifnot(
                  identical(.Random.seed, seed_before),
                  identical(ls(globalenv(), all.names = TRUE), workspace_before)
                )
                writeLines("No package preparation, example effects, or workspace leaks")
                """),
            expected="No package preparation, example effects, or workspace leaks\n",
        )
        return client.finish()


@requires(R, native_euro_locale())
@executions(RUNTIME)
def test_preserves_utf8_in_compact_outputs(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).resolve().parents[3] / "fixtures/r_help"
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        library = directory / "library"
        library.mkdir()
        subprocess.run(
            [
                rscript.with_name("R.exe" if os.name == "nt" else "R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            # Build the index where its euro title is representable as well.
            env=environment | {"LC_ALL": NATIVE_EURO_LOCALE},
            check=True,
            capture_output=True,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(library), environment.get("R_LIBS")))
        )
        with McpClient(
            binary, execution.serve(), environment, current_directory=directory
        ) as client:
            client.initialize_and_list_tools()
            # Renderable in native encoding, but Console receives UTF-8 bytes.
            client.expect(
                r='help_locale <- Sys.getlocale("LC_CTYPE")\n'
                f'selected_help_locale <- Sys.setlocale("LC_CTYPE", "{NATIVE_EURO_LOCALE}")\n'
                'stopifnot(nzchar(selected_help_locale), !l10n_info()[["UTF-8"]])',
            )
            client.transcript[-1]["send"]["r"] = client.transcript[-1]["send"][
                "r"
            ].replace(NATIVE_EURO_LOCALE, "<native euro locale>")
            client.send(
                r=recipe("quick-topic")
                .replace('package <- "stats"', 'package <- "mcpconsolehelp"')
                .replace('topic <- "lm"', 'topic <- "currency"'),
            )
            excerpt = last_tool_text(client)
            client.send(
                r=recipe("candidates")
                .replace('"^plot$"', '"^currency$"')
                .replace('c("base", "graphics")', '"mcpconsolehelp"'),
            )
            candidates = last_tool_text(client)
            assert "Currency €2.50" in excerpt and candidates == (
                "mcpconsolehelp::currency | Currency €2.50\n"
            ), (excerpt, candidates)
            assert len(excerpt.encode("utf-8")) < 8192, excerpt
            assert excerpt.endswith("[Excerpt; full text retained in help_text.]\n")
            client.expect(r='invisible(Sys.setlocale("LC_CTYPE", help_locale))')
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
