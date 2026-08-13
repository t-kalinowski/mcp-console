#!/usr/bin/env -S uv run --script

import os
import shutil
import tempfile
from pathlib import Path

from _support import (
    McpClient,
    Transcript,
    code,
    normalize_python_resolution_error,
    r_test_environment,
    run_this_suite,
)


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir"}


def test_rejects_unsupported_ir_version(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        fake_bin = workspace / "bin"
        fake_bin.mkdir()
        fake_ir = fake_bin / "ir"
        fake_ir.write_text(
            code(r"""
                #!/bin/sh

                set -eu
                if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
                  count=0
                  if [ -e "$MCP_CONSOLE_IR_VERSION_COUNT" ]; then
                    read -r count < "$MCP_CONSOLE_IR_VERSION_COUNT"
                  fi
                  count=$((count + 1))
                  printf '%s\n' "$count" > "$MCP_CONSOLE_IR_VERSION_COUNT"
                  if [ "$count" -eq 1 ]; then
                    printf 'ir 0.4.0\n'
                  else
                    printf 'ir 0.3.0\n'
                  fi
                  exit 0
                fi
                if [ "$(cat "$MCP_CONSOLE_IR_VERSION_COUNT")" -gt 1 ]; then
                  printf 'started\n' > "$MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"
                fi
                printf '%s' "$MCP_CONSOLE_FAKE_R_LIBRARY"
                """),
            encoding="utf-8",
        )
        fake_ir.chmod(0o755)
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        run_marker = workspace / "unsupported-ir-ran"
        environment["MCP_CONSOLE_IR_VERSION_COUNT"] = str(
            workspace / "ir-version-count"
        )
        environment["MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"] = str(run_marker)
        environment["MCP_CONSOLE_FAKE_R_LIBRARY"] = str(workspace)

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"r": ["local::package"]},
        )
        result = client.transcript[-1]["result"]
        assert not run_marker.exists(), "unsupported IR reached package resolution"
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "R package resolution requires ir 0.4.0 or later; found ir 0.3.0"
        ), result
        return client._finish()


def test_rejects_local_r_installation(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    environment.pop("IR_NO_LOCAL_SOURCES", None)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "r_install_escape"
        package = workspace / "package"
        shutil.copytree(fixture, package)
        (package / "inst").mkdir()
        (package / "inst" / "nonce").write_text(str(workspace), encoding="utf-8")

        install_marker = workspace / "package-configure-ran"
        environment["MCP_CONSOLE_R_INSTALL_MARKER"] = str(install_marker)

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        reference = f"local::{package}?reinstall&nocache"
        client.session(action="prepare", requirements={"r": [reference]})
        result = client.transcript[-1]["result"]
        assert not install_marker.exists(), (
            "local package configure ran with server permissions"
        )
        assert result["isError"] is True, result
        error = result["content"][0]["text"]
        assert "IR_NO_LOCAL_SOURCES is set" in error, error
        assert "mcpconsolerinstallescape" in error, error
        assert "Use a remote package source" in error, error
        diagnostic = "Error: IR_NO_LOCAL_SOURCES is set"
        _, separator, error = error.partition(diagnostic)
        assert separator, error
        error = f"R package resolution failed with exit status: 1: {diagnostic}{error}"
        client.transcript[-1]["session"]["requirements"]["r"] = [
            reference.replace(str(package), "<absolute package path>")
        ]
        result["content"][0]["text"] = error.replace(
            str(package), "<absolute package path>"
        )
        return client._finish()


def test_prepares_and_uses_cran_packages(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"r": ["praise, zeallot"]},
    )
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(dirname(find.package("praise")), .libPaths()[[1L]]),
          identical(dirname(find.package("zeallot")), .libPaths()[[1L]])
        )
        result <- dplyr::summarise(
          data.frame(value = c(40L, 2L)),
          answer = sum(.data$value)
        )
        praise::praise(sprintf("answer: %d", result$answer))
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "answer: 42"\n'
    return client._finish()


def test_prepares_r_requirements_after_worker_startup(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(action="prepare", requirements={"r": ["praise"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        initial_library <- .libPaths()[[1L]]
        stopifnot(
          !file.exists(file.path(initial_library, "zeallot", "DESCRIPTION"))
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    client.session(action="prepare", requirements={"r": ["zeallot"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          identical(dirname(find.package("zeallot")), .libPaths()[[1L]])
        )
        42L
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client._finish()


def test_failed_late_mixed_preparation_preserves_worker(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(action="prepare", requirements={"r": ["praise"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        initial_library <- .libPaths()[[1L]]
        stopifnot(
          !file.exists(file.path(initial_library, "zeallot", "DESCRIPTION"))
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    invalid_python = "not a valid requirement !!!"
    client.session(
        action="prepare",
        requirements={
            "r": ["zeallot"],
            "python": [invalid_python],
        },
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    result["content"][0]["text"] = normalize_python_resolution_error(
        result["content"][0]["text"], invalid_python
    )

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          identical(.libPaths()[[1L]], initial_library),
          !file.exists(file.path(initial_library, "zeallot", "DESCRIPTION")),
          !"not a valid requirement !!!" %in%
            reticulate::py_require()$packages
        )
        42L
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client._finish()


def test_evaluates_with_default_managed_r(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(ambient_library), environment.get("R_LIBS")))
        )

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            stopifnot(
              identical(dirname(find.package("tidyverse")), .libPaths()[[1L]]),
              identical(dirname(find.package("reticulate")), .libPaths()[[1L]]),
              identical(dirname(find.package("DBI")), .libPaths()[[1L]]),
              identical(dirname(find.package("duckdb")), .libPaths()[[1L]]),
              identical(dirname(find.package("arrow")), .libPaths()[[1L]]),
              identical(dirname(find.package("nanoarrow")), .libPaths()[[1L]]),
              identical(packageDescription("reticulate")$RemoteType, "github"),
              nzchar(packageDescription("reticulate")$RemoteSha),
              vapply(
                c("ggplot2", "dplyr", "readr", "jsonlite"),
                requireNamespace,
                logical(1L),
                quietly = TRUE
              )
            )
            csv <- readr::read_csv(I("value\n40\n2\n"), show_col_types = FALSE)
            json <- jsonlite::fromJSON('[{"value": 40}, {"value": 2}]')
            stopifnot(identical(as.integer(csv$value), json$value))
            dplyr::summarise(csv, answer = sum(.data$value))$answer
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] 42\n", client.transcript[-1]
        client.session(
            action="prepare",
            requirements={"r": ["DBI", "duckdb", "arrow", "nanoarrow"]},
        )
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        return client._finish()


def test_prepares_initial_r_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    initial_r = "praise"
    candidate_r = "zeallot"
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(ambient_library), environment.get("R_LIBS")))
        )
        environment["MCP_CONSOLE_AMBIENT_R_LIBRARY"] = str(ambient_library)

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"

        invalid_r = "not a valid requirement !!!"
        client.session(
            action="prepare",
            requirements={"r": [invalid_r]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        error = result["content"][0]["text"]
        assert error.startswith(
            "R package resolution failed with exit status: 1: Error:"
        ), error
        assert f"Cannot parse package: {invalid_r}." in error, error
        assert error.endswith("Execution halted\nir: dependency resolution failed"), (
            error
        )
        result["content"][0]["text"] = "\n".join(
            line.rstrip() for line in error.splitlines()
        )

        invalid_python = "not a valid requirement !!!"
        client.session(
            action="prepare",
            requirements={
                "r": [candidate_r],
                "python": [invalid_python],
            },
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        result["content"][0]["text"] = normalize_python_resolution_error(
            result["content"][0]["text"], invalid_python
        )

        # fmt: r
        r = code(r"""
            stopifnot(
              identical(
                dirname(find.package("praise")),
                .libPaths()[[1L]]
              ),
              !file.exists(
                file.path(.libPaths()[[1L]], "zeallot", "DESCRIPTION")
              ),
              normalizePath(.libPaths()[[2L]]) ==
                normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
            )
            42L
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] 42\n"

        client.session(
            action="prepare",
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"
        client.session(
            action="prepare",
            requirements={
                "r": [candidate_r],
                "python": ["py-yaml12"],
            },
        )
        assert last_tool_text(client) == "[prepared]"

        # fmt: r
        prepared_r = code(r"""
            stopifnot(
              "py-yaml12" %in% reticulate::py_require()$packages,
              identical(
                dirname(find.package("praise")),
                .libPaths()[[1L]]
              ),
              identical(
                dirname(find.package("zeallot")),
                .libPaths()[[1L]]
              ),
              normalizePath(.libPaths()[[2L]]) ==
                normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
            )
            42L
            """)
        client.send(r=prepared_r)
        assert last_tool_text(client) == "[1] 42\n"

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(r=prepared_r)
        assert last_tool_text(client) == "[1] 42\n"
        return client._finish()


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    content = result["content"]
    assert len(content) == 1, content
    assert content[0]["type"] == "text", content
    return content[0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
