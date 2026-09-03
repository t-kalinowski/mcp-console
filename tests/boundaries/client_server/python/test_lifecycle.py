#!/usr/bin/env -S uv run --script

import json
import os
import plistlib
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    assert_result_content,
    checkpoint_uv_environment,
    code,
    r_test_environment,
    reference_plots,
    release_worker_callback_gate,
    run_this_suite,
    stop_client,
    wait_for_evaluation_output,
    wait_for_idle_output,
    wait_for_worker_file,
)

PLATFORMS = {"darwin"}
PYTHON_DOWNLOAD_URL = "https://example.invalid/python.tar.zst"


from client_server._harness import (
    _python_last_tool_text as last_tool_text,
    named_requirement_error,
)


def test_rejects_python_preparation_while_evaluation_is_running(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        uv_record = temporary / "uv-record.jsonl"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        python = code("""
            runtime_generation_marker = "original runtime retained"
            preparation_gate = input("preparation gate> ")
            """)
        client.send(python=python)
        assert last_tool_text(client) == (
            '[input requested: "preparation gate> "]\n[waiting for stdin]'
        )
        uv_record.write_text("", encoding="utf-8")

        preparation_returned = threading.Event()

        def stop_blocked_preparation() -> None:
            # This is only a deadlock guard. The blocked input, not elapsed
            # time, proves that a successful preparation response was prompt.
            if not preparation_returned.wait(30):
                client.process.kill()

        watchdog = threading.Thread(target=stop_blocked_preparation)
        watchdog.start()
        try:
            client.send(
                requirements={"python": ["py-yaml12"]},
            )
        finally:
            preparation_returned.set()
            watchdog.join()
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "worker is already evaluating a cell; poll it before preparing requirements"
        )
        assert uv_record.read_text(encoding="utf-8") == ""

        combined = client.send(
            python="combined_cell_ran = True",
            requirements={"python": ["py-yaml12"]},
        )
        assert combined["isError"] is True, combined
        assert combined["content"][0]["text"] == (
            "worker is already evaluating a cell; poll it before preparing requirements"
        )
        assert uv_record.read_text(encoding="utf-8") == ""

        client.send(stdin="continue\n")
        output = last_tool_text(client)
        assert output == "[done]", repr(output)
        client.send(
            python=("runtime_generation_marker, 'combined_cell_ran' not in globals()")
        )
        assert last_tool_text(client) == "('original runtime retained', True)\n"
        return client._finish()


def test_interrupts_running_python_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::trace(
                  "py_eval",
                  tracer = quote({
                    invisible(file.create(file.path(
                      tempdir(),
                      "python-r-interrupt-started"
                    )))
                    repeat {
                      Sys.sleep(60)
                    }
                  }),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "[done]", repr(output)

            client.send(python="42", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-r-interrupt-started",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_tool_text(client)
            assert output == "\n", repr(output)

            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::untrace(
                  "py_eval",
                  where = asNamespace("reticulate")
                )))
                # Poison reticulate's cached result wrapper after MCP Console
                # initializes its private Python evaluator. Cell results must
                # still return through direct conversion instead of that wrapper.
                invisible(reticulate::py_eval(
                  r"---(
                exec(
                    "import inspect\n"
                    "inspect._mcp_original_getmro_code = inspect.getmro.__code__\n"
                    "def _mcp_interrupting_getmro(cls):\n"
                    "    getmro.__code__ = _mcp_original_getmro_code\n"
                    "    raise KeyboardInterrupt\n"
                    "inspect.getmro.__code__ = _mcp_interrupting_getmro.__code__\n"
                )
                )---",
                  convert = TRUE
                ))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"

            # fmt: python
            python = code("""
                import inspect
                import os
                import time
                from pathlib import Path

                inspect.getmro.__code__ = inspect._mcp_original_getmro_code
                python_interrupt_state = 41
                Path(
                    os.environ["TMPDIR"],
                    "python-interrupt-started",
                ).touch()
                while True:
                    time.sleep(60)
                """)
            client.send(python=python, timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-interrupt-started",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(python="python_interrupt_state + 1")
            assert last_tool_text(client) == "42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_initializes_private_runtime_once_on_first_python_cell(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        length(getHook("reticulate::matplotlib.pyplot::load"))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 1\n"
    client.send(python="42")
    assert last_tool_text(client) == "42\n"
    # fmt: r
    r = code(r"""
        length(getHook("reticulate::matplotlib.pyplot::load"))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 1\n"
    return client._finish()


def test_retries_python_runtime_initialization_after_interrupt(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::trace(
                  "py_set_attr",
                  tracer = quote({
                    if (
                      identical(name, "operation") &&
                        identical(value, "configure_import_resolution")
                    ) {
                      invisible(file.create(file.path(
                        tempdir(),
                        "python-runtime-configuring"
                      )))
                      repeat {
                        Sys.sleep(60)
                      }
                    }
                  }),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"

            client.send(python="42", timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-runtime-configuring",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_tool_text(client)
            assert output in {"", "\n"}, repr(output)
            result["content"][0]["text"] = output.rstrip("\n")

            # fmt: r
            r = code(r"""
                invisible(suppressMessages(base::untrace(
                  "py_set_attr",
                  where = asNamespace("reticulate")
                )))
                length(getHook("reticulate::matplotlib.pyplot::load"))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[1] 1\n"

            client.send(python="42")
            output = last_tool_text(client)
            assert output == "42\n", repr(output)
            client.send(python="import yaml12; yaml12.__name__")
            output = last_tool_text(client)
            assert output == (
                "[resolved PyPI distribution 'py-yaml12' "
                "for Python import 'yaml12']\n"
                "'yaml12'\n"
            ), repr(output)
            # fmt: python
            python = code("""
                import logging

                sum(
                    getattr(filter_, "_mcp_console_filter", False)
                    for filter_ in logging.getLogger("matplotlib.font_manager").filters
                )
                """)
            client.send(python=python)
            assert last_tool_text(client) == "1\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_dispatch_does_not_mutate_python_globals(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import threading

        globals_iteration_started = threading.Event()
        globals_iteration_continue = threading.Event()
        globals_iteration_result = []


        def iterate_globals():
            iterator = iter(globals())
            next(iterator)
            globals_iteration_started.set()
            globals_iteration_continue.wait()
            try:
                tuple(iterator)
                globals_iteration_result.append("stable")
            except BaseException as error:
                globals_iteration_result.append(repr(error))


        globals_iteration_thread = threading.Thread(target=iterate_globals)
        globals_iteration_thread.start()
        globals_iteration_started.wait()
        None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        globals_iteration_continue.set()
        globals_iteration_thread.join()
        globals_iteration_result
        """)
    client.send(python=python)
    assert last_tool_text(client) == "['stable']\n"
    return client._finish()


def test_interrupts_live_python_resolver(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "mcp-console-blocked-live-preparation"
        )
        uv_interrupted = FifoCheckpoint(temporary / "uv-interrupted")
        uv_interrupt_release = FifoCheckpoint(temporary / "uv-interrupt-release")
        environment["MCP_CONSOLE_TEST_UV_INTERRUPTED"] = str(uv_interrupted.path)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPT_RELEASE"] = str(
            uv_interrupt_release.path
        )
        environment["RUST_LOG"] = "error"
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            client = McpClient(binary, ("serve",), environment)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        passed = False
        interrupt_released = False
        try:
            client._initialize_and_list_tools()
            client.send(r="resolver_interrupt_state <- 41L")
            assert last_tool_text(client) == "[done]"

            preparation = client._start_send(
                r="resolver_interrupt_cell_ran <- TRUE",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            interrupt = client._start_send(control="interrupt", timeout_ms=0)
            uv_interrupted.wait("live Python resolver interrupt")
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert client.stdout in readable, (
                "control-only interrupt waited for Python preparation to settle"
            )
            client._receive(interrupt)
            assert interrupt["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, interrupt

            uv_interrupt_release.release()
            interrupt_released = True
            client._receive(preparation)
            assert preparation["result"]["isError"] is True, preparation
            error = preparation["result"]["content"][0]["text"]
            assert "managed Python resolution" in error, error
            preparation["result"]["content"][0]["text"] = (
                "managed Python resolution cancelled by interrupt"
            )

            client.send()
            assert last_tool_text(client) == "\n[idle]"

            client.send(
                r=(
                    "resolver_interrupt_state + "
                    "as.integer(!exists('resolver_interrupt_cell_ran'))"
                )
            )
            assert last_tool_text(client) == "[1] 42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not interrupt_released:
                uv_interrupt_release.release()
            uv_release.release()
            uv_started.close()
            uv_release.close()
            uv_interrupted.close()
            uv_interrupt_release.close()
            if not passed:
                stop_client(client)


def test_restart_cancels_live_python_preparation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "mcp-console-blocked-live-preparation"
        )
        client = McpClient(binary, ("serve",), environment)
        passed = False
        try:
            client._initialize_and_list_tools()
            client.send(r="restart_marker <- 42L")
            assert last_tool_text(client) == "[done]"

            preparation = client._start_send(
                r="stop('cancelled requirements cell ran')",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            calls_returned = threading.Event()
            forced_release = threading.Event()

            def release_if_calls_block() -> None:
                if not calls_returned.wait(2):
                    forced_release.set()
                    uv_release.release()

            watchdog = threading.Thread(target=release_if_calls_block)
            watchdog.start()
            poll = client._start_send()
            second_prepare = client._start_send(
                requirements={"python": ["py-yaml12"]},
            )
            client._receive_many([poll, second_prepare])
            calls_returned.set()
            watchdog.join()
            assert not forced_release.is_set(), (
                "another tool call waited for live preparation"
            )
            assert poll["result"] == {
                "content": [
                    {"type": "text", "text": "[session is preparing requirements]"}
                ],
                "isError": True,
            }, poll
            assert second_prepare["result"] == {
                "content": [
                    {"type": "text", "text": "session is preparing requirements"}
                ],
                "isError": True,
            }, second_prepare

            restart = client._start_send(control="restart")
            client._receive_many([preparation, restart])

            preparation_result = preparation["result"]
            assert preparation_result == {
                "content": [
                    {
                        "type": "text",
                        "text": "Python preparation cancelled by restart",
                    }
                ],
                "isError": True,
            }, preparation_result
            assert restart["result"]["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n[idle]"
                    ),
                }
            ], restart

            # fmt: r
            r = code(r"""
                requirements <- reticulate::py_require()
                stopifnot(
                  !exists("restart_marker", inherits = FALSE),
                  !"mcp-console-blocked-live-preparation" %in% requirements$packages
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            uv_release.release()
            uv_started.close()
            uv_release.close()
            if not passed:
                stop_client(client)


def test_does_not_parse_requirements_as_rscript_options(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        marker = Path(temporary_directory) / "host-r-code-ran"
        expression = (
            "base::writeLines('executed', base::Sys.getenv('MCP_CONSOLE_HOST_MARKER'))"
        )
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_HOST_MARKER"] = str(marker)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.send(
            requirements={"python": ["-e", expression]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert not marker.exists(), "requirement executed as unsandboxed R code"
        assert result["content"][0]["text"] == named_requirement_error("-e")
        return client._finish()


def test_forces_uv_offline_in_builtin_worker(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("UV_OFFLINE", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "1"\n'
    return client._finish()


if __name__ == "__main__":
    run_this_suite(__file__)
