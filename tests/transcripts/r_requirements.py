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


def test_rejects_local_r_requirements_before_ir_starts(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    real_ir = shutil.which("ir")
    assert real_ir is not None

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        service = workspace / "service"
        service.mkdir()
        fixture = Path(__file__).resolve().parents[1] / "fixtures" / "r_install_escape"
        package = service / "package"
        shutil.copytree(fixture, package)
        (package / "inst").mkdir()
        (package / "inst" / "nonce").write_text(str(workspace), encoding="utf-8")
        (workspace / "package").symlink_to(package, target_is_directory=True)

        install_marker = workspace / "package-configure-ran"
        ir_marker = workspace / "ir-started"
        wrapper_directory = workspace / "bin"
        wrapper_directory.mkdir()
        ir_wrapper = wrapper_directory / "ir"
        ir_wrapper.write_text(
            code(r"""
                #!/bin/sh
                set -eu
                : > "$MCP_CONSOLE_IR_STARTED"
                exec "$MCP_CONSOLE_REAL_IR" "$@"
                """),
            encoding="utf-8",
        )
        ir_wrapper.chmod(0o755)
        environment["PATH"] = os.pathsep.join(
            (str(wrapper_directory), environment["PATH"])
        )
        environment["MCP_CONSOLE_REAL_IR"] = real_ir
        environment["MCP_CONSOLE_IR_STARTED"] = str(ir_marker)
        environment["MCP_CONSOLE_R_INSTALL_MARKER"] = str(install_marker)

        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=service,
        )
        client._initialize_and_list_tools()
        absolute = str(package)
        references = [
            f"local::{absolute}?reinstall&nocache",
            f"mcpconsolerinstallescape=local::{absolute}?reinstall&nocache",
            "./package?reinstall&nocache",
            "mcpconsolerinstallescape=./package?reinstall&nocache",
            "../package?reinstall&nocache",
            f"{absolute}?reinstall&nocache",
            f"mcpconsolerinstallescape={absolute}?reinstall&nocache",
            "cli, mcpconsolerinstallescape=local::./package?reinstall&nocache",
            "deps::./package",
            "mcpconsolerinstallescape=url::file:///tmp/package.tar.gz",
            "git::file://localhost/tmp/package",
        ]
        for reference in references:
            client.session(
                action="prepare",
                requirements={"r": [reference]},
            )
            result = client.transcript[-1]["result"]
            assert not install_marker.exists(), (
                "local package configure ran with server permissions"
            )
            assert not ir_marker.exists(), (
                f"IR started before rejecting a local reference: {result}"
            )
            assert result["isError"] is True, result
            assert result["content"][0]["text"] == (
                "local R package references are not supported"
            )
            if absolute in reference:
                client.transcript[-1]["session"]["requirements"]["r"] = [
                    reference.replace(absolute, "<absolute package path>")
                ]
        return client._finish()


def test_prepares_and_uses_cran_packages(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"r": ["cli, dplyr"]},
    )
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(dirname(find.package("cli")), .libPaths()[[1L]]),
          identical(dirname(find.package("dplyr")), .libPaths()[[1L]])
        )
        result <- dplyr::summarise(
          data.frame(value = c(40L, 2L)),
          answer = sum(.data$value)
        )
        cli::format_inline("answer: {result$answer}")
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "answer: 42"\n'
    return client._finish()


def test_prepares_initial_r_requirements(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    initial_r = "cli"
    candidate_r = "glue"
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
                dirname(find.package("cli")),
                .libPaths()[[1L]]
              ),
              !file.exists(
                file.path(.libPaths()[[1L]], "glue", "DESCRIPTION")
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
            requirements={"r": [candidate_r]},
        )
        assert last_tool_text(client) == "[restart required]"

        client.session(action="restart")
        assert last_tool_text(client) == "[restarted]"
        client.send(r=r)
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
