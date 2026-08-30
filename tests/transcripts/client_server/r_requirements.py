#!/usr/bin/env -S uv run --script

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    Transcript,
    checkpoint_uv_environment,
    code,
    r_test_environment,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_idle_output,
)

PLATFORMS = {"darwin", "linux"}
REQUIRED_COMMANDS = {"ir"}


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


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
                  printf 'ir 0.3.0\n'
                  exit 0
                fi
                printf 'started\n' > "$MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"
                exit 97
                """),
            encoding="utf-8",
        )
        fake_ir.chmod(0o755)
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        run_marker = workspace / "unsupported-ir-ran"
        environment["MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"] = str(run_marker)
        zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"

        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.send(
            requirements={"r": ["local::package"]},
        )
        result = client.transcript[-1]["result"]
        assert not run_marker.exists(), "unsupported `ir` reached package resolution"
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "R package resolution requires `ir` 0.4.0 or later; found `ir` 0.3.0"
        ), result
        return client._finish()


def test_rejects_local_r_installation(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    environment.pop("IR_NO_LOCAL_SOURCES", None)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        fixture = Path(__file__).resolve().parents[2] / "fixtures" / "r_install_escape"
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
        client.send(requirements={"r": [reference]})
        result = client.transcript[-1]["result"]
        assert not install_marker.exists(), (
            "local package configure ran with server permissions"
        )
        assert result["isError"] is True, result
        error = result["content"][0]["text"]
        assert "IR_NO_LOCAL_SOURCES is set" in error, error
        assert "mcpconsolerinstallescape" in error, error
        assert "Use a remote package source" in error, error
        progress, diagnostic_start, diagnostic = error.partition(
            "Error: IR_NO_LOCAL_SOURCES is set"
        )
        failure_prefix = "R package resolution failed with exit status: 1: "
        assert progress.startswith(failure_prefix), error
        assert diagnostic_start and "Resolving" in progress, error
        # `ir` may load cached metadata or refresh it before the same rejection.
        error = (
            f"{failure_prefix}<cache-dependent `ir` progress>\n"
            f"{diagnostic_start}{diagnostic}"
        )
        client.transcript[-1]["send"]["requirements"]["r"] = [
            reference.replace(str(package), "<absolute package path>")
        ]
        result["content"][0]["text"] = error.replace(
            str(package), "<absolute package path>"
        )
        client.transcript[-1]["transcript_normalization"] = {
            "target": "result.content[0].text",
            "replacements": {
                "cache_dependent_ir_progress": "<cache-dependent `ir` progress>",
            },
        }
        return client._finish()


def test_prepares_and_uses_cran_packages(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(
        requirements={"r": ["praise, zeallot"]},
    )
    assert last_tool_text(client) == "[prepared]", client.transcript[-1]

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


def test_sends_r_cell_with_initial_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(dirname(find.package("praise")), .libPaths()[[1L]])
        )
        praise::praise("ready")
        """)
    client.send(r=r, requirements={"r": ["praise"]})
    output = last_tool_text(client)
    assert output.startswith('[1] "') and "ready" in output, output
    assert "[prepared]" not in output, output
    return client._finish()


def test_prepares_r_requirements_after_worker_startup(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["praise"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        initial_library <- .libPaths()[[1L]]
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          !initial_library %in% .libPaths(),
          identical(dirname(find.package("zeallot")), .libPaths()[[1L]])
        )
        42L
        """)
    client.send(r=r, requirements={"r": ["zeallot"]})
    assert last_tool_text(client) == "[1] 42\n"
    return client._finish()


def test_stops_live_preparation_for_idle_callback_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        reticulate::py_require("py-yaml12")
        invisible(reticulate::py_config())
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          # later caps one top-level handler turn at 20 callback passes.
          # Leave the input callback ready for the next handler turn.
          request_input <- function(turns) {
            if (turns == 0L) {
              stopifnot(file.create(callback_checkpoint))
              readline("later> ")
              # Keep the submitted callback alive if relay retirement closes
              # fd 0 before the force-stop signal reaches the worker.
              repeat {
                Sys.sleep(1)
              }
            } else {
              later::later(function() request_input(turns - 1L), delay = 0)
            }
          }
          request_input(25L)
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle input callback")
    wait_for_idle_output(
        client,
        '[input requested: "later> "]\n[waiting for stdin]',
        "idle callback input request",
    )
    # Keep this distinct from the callback's retained requirement so activation
    # cannot turn the preparation into an idempotent server-side no-op.
    result = client.send(
        requirements={"python": ["py-yaml12>=0"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        '[idle R callback requested input "later> " during requirement '
        "preparation; collect callback input with send before preparing "
        "requirements]\n[worker terminated by signal 9]\n"
        "[worker stopped: in-memory state lost]"
    ), result
    return client._finish()


def test_failed_mixed_preparation_retains_live_python_activation(
    binary: Path,
) -> Transcript:
    requirement = "mcpconsolepreparationfixture"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "py-yaml12"
        )
        r_environment, _ = r_test_environment()
        environment["R_HOME"] = r_environment["R_HOME"]

        real_ir = shutil.which("ir")
        assert real_ir is not None, "real `ir` is required"
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(
            Path(__file__).resolve().parents[2] / "fixtures" / "selective_ir"
        )
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        candidate = temporary / "candidate-r-library"
        candidate.mkdir()
        environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
        environment["MCP_CONSOLE_TEST_IR_REQUIREMENT"] = requirement
        environment["MCP_CONSOLE_TEST_IR_LIBRARY"] = str(candidate)

        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            setup = code(r"""
                invisible(reticulate::py_config())
                cat(.libPaths()[[1L]])
                """)
            client.send(r=setup)
            initial_library = Path(last_tool_text(client))
            assert initial_library.is_dir(), initial_library
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<initial managed R library>"
            )
            for package in initial_library.iterdir():
                (candidate / package.name).symlink_to(
                    package,
                    target_is_directory=package.is_dir(),
                )

            preparation = client._start_send(
                requirements={"r": [requirement], "python": ["py-yaml12"]},
            )
            uv_started.wait("mixed live requirement preparation")
            for package in candidate.iterdir():
                package.unlink()
            candidate.rmdir()
            candidate.write_text("not an R library", encoding="utf-8")
            uv_release.release()
            client._receive(preparation)
            result = preparation["result"]
            assert result["isError"] is True, result
            assert result["content"][0]["text"] == (
                "resolved R library was not added to .libPaths(); further "
                "requirement changes are unavailable until session restart"
            )

            client.send(control="restart")
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            # fmt: python
            python = code("""
                import yaml12

                yaml12.format_yaml({"answer": 42})
                """)
            client.send(python=python)
            assert last_tool_text(client) == "'answer: 42'\n", client.transcript[-1]
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            uv_release.release()
            uv_started.close()
            uv_release.close()
            if not passed:
                stop_client(client)


def test_failed_late_mixed_preparation_preserves_worker(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(requirements={"r": ["praise"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        initial_lib_paths <- .libPaths()
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", client.transcript[-1]

    invalid_python = "example @ https://example.invalid/example.whl"
    client.send(
        requirements={
            "r": ["zeallot"],
            "python": [invalid_python],
            "duckdb": ["not_a_real_duckdb_extension"],
        },
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid_python)

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          identical(.libPaths(), initial_lib_paths)
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
        client.send(
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
        client.send(
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"

        invalid_r = "not a valid requirement !!!"
        client.send(
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

        invalid_python = "example @ https://example.invalid/example.whl"
        client.send(
            requirements={
                "r": [candidate_r],
                "python": [invalid_python],
            },
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == named_requirement_error(invalid_python)

        # fmt: r
        r = code(r"""
            stopifnot(
              identical(
                dirname(find.package("praise")),
                .libPaths()[[1L]]
              ),
              normalizePath(.libPaths()[[2L]]) ==
                normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
            )
            42L
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] 42\n"

        client.send(
            requirements={"r": [initial_r]},
        )
        assert last_tool_text(client) == "[prepared]"
        client.send(
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

        client.send(control="restart")
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
