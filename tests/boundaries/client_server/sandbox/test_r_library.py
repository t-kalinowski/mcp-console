#!/usr/bin/env -S uv run --script

import re
import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_installs_package_in_temporary_r_library(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        package = workspace / "mcpconsolelocalpkg"
        (package / "R").mkdir(parents=True)
        (package / "DESCRIPTION").write_text(
            """\
Package: mcpconsolelocalpkg
Title: Local R Installation Fixture
Version: 0.0.1
Authors@R: person("Test", "Fixture", email = "fixture@example.com", role = c("aut", "cre"))
Description: Verifies default R package installation inside the sandbox.
License: MIT
Encoding: UTF-8
""",
            encoding="utf-8",
        )
        (package / "NAMESPACE").write_text("export(answer)\n", encoding="utf-8")
        (package / "R" / "answer.R").write_text(
            # fmt: r
            code("""
                answer <- function() 42L
                """),
            encoding="utf-8",
        )
        (package / "man").mkdir()
        (package / "man" / "answer.Rd").write_text(
            r"""\name{answer}
\alias{answer}
\title{Return the fixture answer}
\usage{answer()}
\description{Returns 42.}
""",
            encoding="utf-8",
        )
        archive = workspace / "mcpconsolelocalpkg_0.0.1.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            tar.add(package, arcname=package.name)

        with McpClient(binary, ("serve",), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: r
                r=code(r"""
                    library <- .libPaths()[[1L]]
                    stopifnot(
                      identical(Sys.getenv("MCP_CONSOLE_SANDBOX"), "1"),
                      startsWith(
                        library,
                        paste0(normalizePath(tempdir()), .Platform$file.sep)
                      ),
                      dir.exists(library),
                      file.access(library, 2L) == 0L
                    )
                    install.packages(
                      "mcpconsolelocalpkg_0.0.1.tar.gz",
                      repos = NULL,
                      type = "source"
                    )
                    stopifnot(
                      identical(dirname(find.package("mcpconsolelocalpkg")), library),
                      identical(mcpconsolelocalpkg::answer(), 42L)
                    )
                    cat("installed in sandbox library\n")
                    """)
            )
            output = last_tool_text(client)
            assert output.endswith("installed in sandbox library\n"), output
            match = re.match(r"Installing package into .(/[^\n]+).\n", output)
            assert match is not None, output
            client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
                match.group(1), "<sandbox R library>"
            )
            client.send(control="restart")
            assert last_tool_text(client).endswith("[starting new worker]\n[idle]")
            client.send(
                # fmt: r
                r=code(r"""
                    stopifnot(
                      startsWith(
                        .libPaths()[[1L]],
                        paste0(normalizePath(tempdir()), .Platform$file.sep)
                      ),
                      dir.exists(.libPaths()[[1L]])
                    )
                    cat("new sandbox library ready\n")
                    """)
            )
            assert last_tool_text(client) == "new sandbox library ready\n", (
                last_tool_text(client)
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
