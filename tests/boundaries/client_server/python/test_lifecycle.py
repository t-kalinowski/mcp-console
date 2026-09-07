#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.normalization import code
from support.records import Transcript
from support.resolvers import checkpoint_uv_environment, named_requirement_error
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


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
        client.initialize_and_list_tools()
        python = code("""
            runtime_generation_marker = "original runtime retained"
            preparation_gate = input("preparation gate> ")
            """)
        client.send(python=python)
        assert last_result_text(client) == (
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

        wait_for_evaluation_output(
            client,
            "[done]",
            "Python input completion",
            stdin="continue\n",
        )
        client.send(
            python=("runtime_generation_marker, 'combined_cell_ran' not in globals()")
        )
        assert last_result_text(client) == "('original runtime retained', True)\n"
        return client.finish()


def test_interrupts_running_python_evaluation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        library = temporary_path / "python-interrupt-checkpoint.dylib"
        source = (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "native"
            / "python_probe_checkpoint.c"
        )
        subprocess.run(
            [
                "cc",
                "-dynamiclib" if sys.platform == "darwin" else "-shared",
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                library,
                source,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_PYTHON_INTERRUPT_LIBRARY"] = str(library)
        client = McpClient(binary, ("serve",), environment)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                python_interrupt_started <- tempfile("python-interrupt-started-")
                python_interrupt_release <- tempfile("python-interrupt-release-")
                Sys.setenv(
                  MCP_CONSOLE_PYTHON_INTERRUPT_STARTED = python_interrupt_started,
                  MCP_CONSOLE_PYTHON_INTERRUPT_RELEASE = python_interrupt_release
                )
                # Complete Python initialization before arming the dispatch checkpoint.
                invisible(reticulate::py_config())
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
                cat(python_interrupt_started, python_interrupt_release, sep = "\n")
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = last_result_text(client).splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = "<interrupt started>\n<interrupt release>"
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            client.send(python="42", timeout_ms=0)
            assert last_result_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-r-interrupt-started",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_result_text(client)
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
            assert last_result_text(client) == "[done]"

            # fmt: python
            python = code("""
                import inspect

                inspect.getmro.__code__ = inspect._mcp_original_getmro_code

                import ctypes
                import os
                import signal

                checkpoint_library = ctypes.PyDLL(os.environ["MCP_CONSOLE_PYTHON_INTERRUPT_LIBRARY"])
                wait_for_interrupt = checkpoint_library.wait_for_probe_interrupt
                wait_for_interrupt.argtypes = (
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_void_p,
                )
                wait_for_interrupt.restype = ctypes.c_int
                python_interrupt_state = 41
                wakeup_read, wakeup_write = os.pipe()
                os.set_blocking(wakeup_write, False)
                previous_wakeup = signal.set_wakeup_fd(wakeup_write)
                try:
                    with (
                        open(
                            os.environ["MCP_CONSOLE_PYTHON_INTERRUPT_STARTED"],
                            "wb",
                            buffering=0,
                        ) as started,
                        open(
                            os.environ["MCP_CONSOLE_PYTHON_INTERRUPT_RELEASE"],
                            "rb",
                            buffering=0,
                        ) as release,
                    ):
                        assert (
                            wait_for_interrupt(
                                started.fileno(),
                                release.fileno(),
                                wakeup_read,
                                ctypes.pythonapi.PyErr_CheckSignals,
                            )
                            == 0
                        )
                finally:
                    signal.set_wakeup_fd(previous_wakeup)
                    os.close(wakeup_read)
                    os.close(wakeup_write)
                """)
            client.send(python=python, timeout_ms=0)
            assert last_result_text(client) == "\n[running; poll with an empty send]"
            started.wait("Python evaluation entered native interrupt checkpoint")

            client.send(control="interrupt", timeout_ms=0)
            assert last_result_text(client) == "\n[running; poll with an empty send]"
            release.release()
            release = None
            client.send()
            assert "KeyboardInterrupt" in last_result_text(client)

            client.send(python="python_interrupt_state + 1")
            assert last_result_text(client) == "42\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


def test_initializes_private_runtime_once_on_first_python_cell(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        length(getHook("reticulate::matplotlib.pyplot::load"))
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 1\n"
    client.send(python="42")
    assert last_result_text(client) == "42\n"
    # fmt: r
    r = code(r"""
        length(getHook("reticulate::matplotlib.pyplot::load"))
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 1\n"
    return client.finish()


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
            client.initialize_and_list_tools()
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
            assert last_result_text(client) == "[done]"

            client.send(python="42", timeout_ms=0)
            assert last_result_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-runtime-configuring",
                client,
            )

            client.send(control="interrupt", timeout_ms=0)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_result_text(client)
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
            assert last_result_text(client) == "[1] 1\n"

            client.send(python="42")
            output = last_result_text(client)
            assert output == "42\n", repr(output)
            client.send(python="import yaml12; yaml12.__name__")
            output = last_result_text(client)
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
            assert last_result_text(client) == "1\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


def test_dispatch_does_not_mutate_python_globals(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client.initialize_and_list_tools()
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
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        globals_iteration_continue.set()
        globals_iteration_thread.join()
        globals_iteration_result
        """)
    client.send(python=python)
    assert last_result_text(client) == "['stable']\n"
    return client.finish()


def test_interrupts_live_python_resolver(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "mcp-console-blocked-live-preparation"
        )
        uv_interrupted = FifoCheckpoint.create(temporary / "uv-interrupted")
        uv_interrupt_release = FifoCheckpoint.create(temporary / "uv-interrupt-release")
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
            client.initialize_and_list_tools()
            client.send(r="resolver_interrupt_state <- 41L")
            assert last_result_text(client) == "[done]"

            preparation = client.start_send(
                r="resolver_interrupt_cell_ran <- TRUE",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            uv_interrupted.wait("live Python resolver interrupt")
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert client.stdout in readable, (
                "control-only interrupt waited for Python preparation to settle"
            )
            client.receive(interrupt)
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
            client.receive(preparation)
            assert preparation["result"]["isError"] is True, preparation
            error = preparation["result"]["content"][0]["text"]
            assert "managed Python resolution" in error, error
            preparation["result"]["content"][0]["text"] = (
                "managed Python resolution cancelled by interrupt"
            )

            client.send()
            assert last_result_text(client) == "\n[idle]"

            client.send(
                r=(
                    "resolver_interrupt_state + "
                    "as.integer(!exists('resolver_interrupt_cell_ran'))"
                )
            )
            assert last_result_text(client) == "[1] 42\n"
            transcript = client.finish()
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
            client.initialize_and_list_tools()
            client.send(r="restart_marker <- 42L")
            assert last_result_text(client) == "[done]"

            preparation = client.start_send(
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
            poll = client.start_send()
            second_prepare = client.start_send(
                requirements={"python": ["py-yaml12"]},
            )
            client.receive_many([poll, second_prepare])
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

            restart = client.start_send(control="restart")
            client.receive_many([preparation, restart])

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
            assert last_result_text(client) == "[done]"
            transcript = client.finish()
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
        client.initialize_and_list_tools()
        client.send(
            requirements={"python": ["-e", expression]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert not marker.exists(), "requirement executed as unsandboxed R code"
        assert result["content"][0]["text"] == named_requirement_error("-e")
        return client.finish()


def test_forces_uv_offline_in_builtin_worker(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, ("serve",), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("UV_OFFLINE", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "1"\n'
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
