#!/usr/bin/env -S uv run --script

import os
import shutil
import tempfile
from pathlib import Path

from _support import McpClient, Transcript, code, run_this_suite


PLATFORMS = {"darwin"}


def configured_python_environment() -> dict[str, str]:
    environment = os.environ.copy()
    python = shutil.which("python3")
    assert python is not None
    environment["RETICULATE_PYTHON"] = python
    return environment


def test_preserves_configured_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    client.call_tool(
        "send",
        r='Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)',
    )
    assert last_tool_text(client) == '[1] "configured-by-user"\n'
    return client.finish()


def test_preserves_empty_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    client.call_tool(
        "send",
        r='Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)',
    )
    assert last_tool_text(client) == '[1] ""\n'
    return client.finish()


def managed_python_transcript(binary: Path, configured: bool) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        marker = Path(temporary_directory) / "managed-python"
        environment = os.environ.copy()
        if configured:
            environment["RETICULATE_PYTHON"] = "managed"
        else:
            environment.pop("RETICULATE_PYTHON", None)
        environment["RETICULATE_UV"] = str(
            Path(__file__).resolve().parents[1] / "fixtures" / "uv"
        )
        python = shutil.which("python3")
        assert python is not None
        environment["MCP_CONSOLE_TEST_PYTHON"] = python
        environment["MCP_CONSOLE_UV_MARKER"] = str(marker)
        environment["UV_OFFLINE"] = "1"

        client = McpClient(binary, ("serve",), environment)
        client.initialize_and_list_tools()
        assert marker.read_text(encoding="utf-8") == "managed Python provisioned\n"
        client.call_tool("send", r="reticulate::py_config()$ephemeral")
        assert last_tool_text(client) == "[1] TRUE\n"
        client.call_tool(
            "send",
            r='reticulate::py_require("mcp-console-test-package")',
        )
        assert last_tool_text(client) == "[done]"
        client.call_tool("send", r="reticulate::py_config()$ephemeral")
        assert last_tool_text(client) == "[1] TRUE\n"
        client.call_tool("send", python="40 + 2")
        output = last_tool_text(client)
        assert output == "42\n", repr(output)
        return client.finish()


def test_evaluates_with_default_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=False)


def test_evaluates_with_explicit_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=True)


def test_forces_uv_offline_in_builtin_worker(binary: Path) -> Transcript:
    environment = configured_python_environment()
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    client.call_tool(
        "send",
        r='Sys.getenv("UV_OFFLINE", unset = NA_character_)',
    )
    assert last_tool_text(client) == '[1] "1"\n'
    return client.finish()


def test_evaluates_cells_in_persistent_reticulate_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        """)
    client.call_tool("send", r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        answer + 1
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output == "from Python\n42\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "2\n"
    client.call_tool("send", python="answer")
    assert last_tool_text(client) == "41\n"
    client.call_tool("send", r="reticulate::py$answer")
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="answer + 1")
    assert last_tool_text(client) == "42\n"
    client.call_tool("send", python="silent = True")
    assert last_tool_text(client) == "[done]"
    return client.finish()


def test_runs_async_python_explicitly(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="asyncio.run(answer())")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def test_recovers_from_python_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert "in fail\n" in output
    assert output.endswith("ValueError: boom\n")
    # fmt: python
    python = code("""
        compile_partial = 9
        await missing()
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.call_tool("send", python='"compile_partial" in globals()')
    assert last_tool_text(client) == "False\n"

    client.call_tool("send", python="nul_state = 42\0")
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert "SyntaxError" in output
    assert "null bytes" in output
    client.call_tool("send", python="answer")
    assert last_tool_text(client) == "41\n"
    return client.finish()


def test_routes_python_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.call_tool("send", python=python)
    assert last_tool_text(client) == '[input requested: "name> "]\n[stdin needed]'
    client.call_tool("send", stdin="Ada\n")
    assert last_tool_text(client) == "'Ada'\n"

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.call_tool("send", python=python, stdin="blue\n")
    assert last_tool_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.call_tool("send", python=python, stdin="fd 0\n")
    assert last_tool_text(client) == "'fd 0\\n'\n"
    return client.finish()


def test_python_debugger_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.call_tool("send", python=python)
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert output.endswith("\n[stdin needed]"), output

    client.call_tool("send", stdin="p debug_value\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 1, output
    assert "41\n" in output, output
    assert output.endswith("\n[stdin needed]"), output

    client.call_tool("send", stdin="continue\n")
    assert last_tool_text(client) == "[done]"
    client.call_tool("send", python="debug_value")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def test_restarts_after_python_bridge_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",), configured_python_environment())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python_worker_marker <- TRUE
        Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
        """)
    client.call_tool("send", r=r)
    client.call_tool("send", python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "Python bridge failed during R evaluation\n"
        "Error in py_discover_config(required_module, use_environment) : \n"
        "  Python specified in RETICULATE_PYTHON "
        "(/mcp-console-missing-python) does not exist\n"
        "[worker sideband read failed: worker sideband closed]"
    )
    client.call_tool("send", r='exists("python_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == (
        "[1] FALSE\n[worker restarted: in-memory state lost]\n"
    )
    client.call_tool("send", python="6 * 7")
    assert last_tool_text(client) == "42\n"
    return client.finish()


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
