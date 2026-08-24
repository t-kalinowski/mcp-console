#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import signal
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    FifoCheckpoint,
    McpClient,
    Transcript,
    checkpoint_uv_environment,
    code,
    normalize_python_resolution_error,
    normalize_python_traceback_paths,
    run_this_suite,
    stop_client,
)

PLATFORMS = {"darwin"}


def recording_uv_environment(
    directory: Path,
    *,
    fail_requirement: str | None = None,
    substitute_requirement: tuple[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["RETICULATE_UV"] = str(
        Path(__file__).resolve().parents[2] / "fixtures" / "record_uv_environment"
    )
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(directory / "uv-environment.jsonl")
    arguments_record = directory / "uv-arguments.jsonl"
    environment["MCP_CONSOLE_TEST_UV_ARGUMENTS_RECORD"] = str(arguments_record)
    if fail_requirement is not None:
        failure_marker = directory / "uv-failure"
        failure_marker.touch()
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = fail_requirement
    if substitute_requirement is not None:
        substitute, replacement = substitute_requirement
        environment["MCP_CONSOLE_TEST_UV_SUBSTITUTE_REQUIREMENT"] = substitute
        environment["MCP_CONSOLE_TEST_UV_REPLACEMENT_REQUIREMENT"] = replacement
    return environment, arguments_record


def uv_tool_run_requirements(record: Path) -> list[list[str]]:
    if not record.exists():
        return []
    arguments = [
        json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()
    ]
    requirements = []
    for invocation in arguments:
        if invocation[:2] != ["tool", "run"]:
            continue
        separator = invocation.index("--")
        manifest = [
            invocation[index + 1]
            for index, argument in enumerate(invocation[:separator])
            if argument == "--with"
        ]
        requirements.append(manifest)
    return requirements


def initialize_python_and_record_baseline(client: McpClient, record: Path) -> int:
    client.send(python="None")
    assert last_tool_text(client) == "[done]"
    return len(uv_tool_run_requirements(record))


def send_and_collect_runtime_python_resolution(
    client: McpClient,
    **arguments: object,
) -> str:
    call_start = len(client.transcript)
    client.send(**arguments)
    chunks = []
    for attempt in range(8):
        output = last_tool_text(client)
        if output.endswith("\n[running]"):
            chunks.append(output.removesuffix("\n[running]"))
            if attempt == 7:
                raise AssertionError(
                    "automatic Python resolution remained running after eight "
                    f"responses: collected={''.join(chunks)!r}, last={output!r}"
                )
            client.send(timeout_ms=30_000)
            continue

        if output != "[done]" or not chunks:
            chunks.append(output)
        collected = "".join(chunks)

        calls = client.transcript[call_start:]
        submitted = calls[0]
        final_result = calls[-1]["result"]
        content = final_result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        content[0]["text"] = collected
        submitted["result"] = final_result
        client.transcript[call_start:] = [submitted]
        return collected
    raise AssertionError("unreachable")


def test_resolves_missing_python_import_without_replaying_cell(
    binary: Path,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()

    client.send(r="automatic_python_r_state <- 42L")
    assert last_tool_text(client) == "[done]"
    client.send(sql="CREATE TABLE automatic_python_state AS SELECT 42 AS answer")

    # fmt: python
    setup = code("""
        import os

        automatic_python_object = {"answer": 42}
        automatic_python_pid = os.getpid()
        """)
    client.send(python=setup)
    assert last_tool_text(client) == "[done]", repr(last_tool_text(client))

    # fmt: python
    python = code("""
        automatic_python_attempts = globals().get("automatic_python_attempts", 0) + 1
        import yaml12

        assert automatic_python_attempts == 1
        automatic_python_input = input()
        (
            yaml12.__name__,
            automatic_python_object["answer"],
            __import__("os").getpid() == automatic_python_pid,
            automatic_python_input,
        )
        """)
    client.send(python=python, stdin="42\n")
    output = last_tool_text(client)
    assert output == ("[input requested: \"\"]\n('yaml12', 42, True, '42')\n"), repr(
        output
    )
    assert "[prepared]" not in output

    client.send(r="automatic_python_r_state")
    assert last_tool_text(client) == "[1] 42\n"
    client.send(sql="SELECT answer FROM automatic_python_state")
    assert last_tool_text(client).splitlines()[-1].split() == ["1", "42"]
    return client._finish()


def test_retries_new_meta_path_finders_after_automatic_resolution(
    binary: Path,
) -> Transcript:
    module = "mcp_console_activated_finder"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            substitute_requirement=(module, "pydash"),
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code(rf"""
            import importlib.util


            class AutomaticMetaLoader:
                def create_module(self, specification):
                    return None

                def exec_module(self, module):
                    module.answer = 42


            class AutomaticMetaFinder:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "{module}":
                        return importlib.util.spec_from_loader(
                            fullname,
                            AutomaticMetaLoader(),
                        )
                    return None


            automatic_meta_finder = AutomaticMetaFinder()
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]"

        # Register the finder only after reticulate activates the inferred
        # environment, while the original import is waiting in this runtime.
        # fmt: r
        r = code(r"""
            reticulate_namespace <- asNamespace("reticulate")
            original_py_require <- get("py_require", envir = reticulate_namespace)
            automatic_meta_finder_registered <- FALSE
            unlockBinding("py_require", reticulate_namespace)
            assign(
              "py_require",
              function(...) {
                result <- original_py_require(...)
                if (!automatic_meta_finder_registered) {
                  reticulate::py_run_string(
                    paste0(
                      "import sys, __main__; ",
                      "sys.meta_path.insert(0, __main__.automatic_meta_finder)"
                    ),
                    local = TRUE
                  )
                  automatic_meta_finder_registered <<- TRUE
                }
                result
              },
              envir = reticulate_namespace
            )
            lockBinding("py_require", reticulate_namespace)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        output = send_and_collect_runtime_python_resolution(
            client,
            python=f"import {module}; {module}.answer",
        )
        assert output == "42\n", repr(output)
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and module in runs[0], runs
        return client._finish()


def test_infers_python_distributions_for_normal_import_forms(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import yaml
            from yaml import safe_load
            from yaml12 import Yaml
            import importlib

            yaml_from_importlib = importlib.import_module("yaml")
            pydash = importlib.import_module("pydash")
            (
                yaml.safe_load("answer: 42"),
                safe_load("answer: 43"),
                yaml_from_importlib is yaml,
                Yaml.__name__,
                pydash.__name__,
            )
            """)
        output = send_and_collect_runtime_python_resolution(client, python=python)
        assert output == (
            "({'answer': 42}, {'answer': 43}, True, 'Yaml', 'pydash')\n"
        ), repr(output)
        assert "[prepared]" not in output

        runs = uv_tool_run_requirements(record)[baseline:]
        inferred = ("pyyaml", "py-yaml12", "pydash")
        assert len(runs) == len(inferred), runs
        for index, (run, requirement) in enumerate(zip(runs, inferred, strict=True)):
            assert run.count(requirement) == 1, run
            for retained in inferred[: index + 1]:
                assert run.count(retained) == 1, run
            assert all(later not in run for later in inferred[index + 1 :]), run
        return client._finish()


def test_does_not_resolve_unreached_or_available_python_imports(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "mcp_console_local_module.py").write_text(
            "answer = 42\n",
            encoding="utf-8",
        )
        (directory / "mcp_console_local_requires_pydash.py").write_text(
            "import pydash\nanswer = pydash.get({'answer': 42}, 'answer')\n",
            encoding="utf-8",
        )
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=directory,
        )
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            if False:
                import mcp_console_not_a_real_package


            def never_called():
                import mcp_console_also_not_a_real_package


            import importlib.util
            import json
            import numpy
            import pandas
            import mcp_console_local_module

            (
                json.loads("42"),
                numpy.__name__,
                pandas.__name__,
                mcp_console_local_module.answer,
                importlib.util.find_spec("mcp_console_probe_missing") is None,
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == ("(42, 'numpy', 'pandas', 42, True)\n"), repr(output)
        runs = uv_tool_run_requirements(record)
        assert len(runs) == baseline, (baseline, runs)

        output = send_and_collect_runtime_python_resolution(
            client,
            python=(
                "import mcp_console_local_requires_pydash; "
                "mcp_console_local_requires_pydash.answer"
            ),
        )
        assert output == "42\n"
        assert len(uv_tool_run_requirements(record)) == baseline + 1

        output = send_and_collect_runtime_python_resolution(
            client,
            python="import yaml12; yaml12.__name__",
        )
        assert output == "'yaml12'\n"
        resolved = len(uv_tool_run_requirements(record))
        assert resolved == baseline + 2

        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client._finish()


def test_does_not_reenter_automatic_python_resolution(binary: Path) -> Transcript:
    nested = "mcp_console_nested_resolution_missing"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # Wrap the public reticulate requirement transition reached by the
        # private callback. Its nested Python miss must not start another host
        # resolver while the outer import owns resolution.
        # fmt: r
        r = code(rf"""
            reticulate_namespace <- asNamespace("reticulate")
            original_py_require <- get("py_require", envir = reticulate_namespace)
            automatic_nested_calls <- 0L
            automatic_nested_error <- NULL
            automatic_nested_triggered <- FALSE
            unlockBinding("py_require", reticulate_namespace)
            assign(
              "py_require",
              function(...) {{
                if (!automatic_nested_triggered) {{
                  automatic_nested_triggered <<- TRUE
                  automatic_nested_calls <<- automatic_nested_calls + 1L
                  automatic_nested_error <<- tryCatch(
                    {{
                      reticulate::py_run_string(
                        "import {nested}",
                        local = TRUE
                      )
                      NA_character_
                    }},
                    error = conditionMessage
                  )
                }}
                original_py_require(...)
              }},
              envir = reticulate_namespace
            )
            lockBinding("py_require", reticulate_namespace)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        output = send_and_collect_runtime_python_resolution(
            client,
            python="import yaml12; yaml12.__name__",
        )
        assert output == "'yaml12'\n"
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and "py-yaml12" in runs[0], runs

        # fmt: r
        r = code(rf"""
            cat(
              automatic_nested_calls,
              grepl("{nested}", automatic_nested_error, fixed = TRUE),
              sep = "\n"
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "1\nTRUE\n", repr(last_tool_text(client))
        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_retains_automatic_python_requirement_after_error_and_restart(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import yaml12

            raise RuntimeError("after automatic Python activation")
            """)
        output = send_and_collect_runtime_python_resolution(client, python=python)
        assert "RuntimeError: after automatic Python activation" in output
        resolved = len(uv_tool_run_requirements(record))
        assert resolved == baseline + 1

        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client._finish()


def test_reports_automatic_python_resolution_failure(binary: Path) -> Transcript:
    requirement = "scikit-learn"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            fail_requirement=requirement,
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        output = send_and_collect_runtime_python_resolution(
            client,
            python="import sklearn",
        )
        for expected in (
            "ModuleNotFoundError",
            "sklearn",
            requirement,
            "synthetic uv failure",
            "requirements.python",
            'requirements: {"python": ["scikit-learn"]}',
            "Import names and PyPI distribution names can differ",
        ):
            assert expected in output, (expected, output)
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1, runs
        assert requirement in runs[0] and "sklearn" not in runs[0], runs

        normalized = normalize_python_resolution_error(output)
        client.transcript[-1]["result"]["content"][0]["text"] = normalized

        client.send(r=f'"{requirement}" %in% reticulate::py_require()$packages')
        assert last_tool_text(client) == "[1] FALSE\n"
        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_retains_inferred_distribution_that_does_not_provide_import(
    binary: Path,
) -> Transcript:
    inferred = "mcp_console_distribution_without_module"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            substitute_requirement=(inferred, "py-yaml12"),
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(python=f"import {inferred}")
        output = last_tool_text(client)
        for expected in (
            "ModuleNotFoundError",
            inferred,
            f"prepared the inferred PyPI distribution `{inferred}`",
            "did not provide the import",
            "requirements.python",
            'requirements: {"python": ["correct-distribution-name"]}',
        ):
            assert expected in output, (expected, output)
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_traceback_paths(output)
        )
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and inferred in runs[0], runs
        resolved = len(uv_tool_run_requirements(record))

        client.send(r=f'"{inferred}" %in% reticulate::py_require()$packages')
        assert last_tool_text(client) == "[1] TRUE\n"
        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(r=f'"{inferred}" %in% reticulate::py_require()$packages')
        assert last_tool_text(client) == "[1] TRUE\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client._finish()


def test_explicit_python_requirements_preempt_automatic_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(
            python="import yaml12; yaml12.__name__",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "'yaml12'\n"
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1, runs
        assert runs[0].count("py-yaml12") == 1, runs
        return client._finish()


def test_requires_explicit_python_requirements_for_ambiguous_or_installed_roots(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = directory / "mcp_console_available_root"
        package.mkdir()
        (package / "__init__.py").write_text("answer = 42\n", encoding="utf-8")
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=directory,
        )
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            try:
                import azure.mcp_console_missing
            except ModuleNotFoundError as error:
                print(f"ambiguous name: {error.name}")
                print(error)

            import mcp_console_available_root

            assert mcp_console_available_root.answer == 42
            try:
                import mcp_console_available_root.missing
            except ModuleNotFoundError as error:
                print(f"installed-root name: {error.name}")
                print(error)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        for expected in (
            "ambiguous name: azure",
            "could not safely infer a PyPI distribution",
            "installed-root name: mcp_console_available_root.missing",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        assert output.count("requirements.python") >= 2, output
        assert len(uv_tool_run_requirements(record)) == baseline
        return client._finish()


def test_reports_unavailable_standard_library_module_without_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import sys

            assert "winreg" in sys.stdlib_module_names
            try:
                import winreg
            except ModuleNotFoundError as error:
                print(f"missing standard-library name: {error.name}")
                print(error)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert "missing standard-library name: winreg" in output, output
        assert "selected Python build" in output, output
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_disables_automatic_resolution_for_user_selected_python(
    binary: Path,
) -> Transcript:
    missing = "mcp_console_user_selected_missing"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        environment["RETICULATE_PYTHON"] = str(Path(sys.executable).resolve())
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(python=f"import {missing}")
        output = last_tool_text(client)
        for expected in (
            "ModuleNotFoundError",
            missing,
            "user-selected Python environment",
            "Automatic managed package resolution is disabled",
            "requirements.python",
            "also disabled",
            "Install the distribution into the selected environment",
            "managed Python",
        ):
            assert expected in output, (expected, output)
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_traceback_paths(output)
        )
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_rejects_automatic_resolution_from_background_thread(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import threading

            thread_result = []


            def import_from_thread():
                import json

                try:
                    import mcp_console_thread_missing
                except ModuleNotFoundError as error:
                    thread_result.extend((json.__name__, error.name, str(error)))


            thread = threading.Thread(target=import_from_thread)
            thread.start()
            thread.join()
            print(f"available module: {thread_result[0]}")
            print(f"background-thread name: {thread_result[1]}")
            print(thread_result[2])
            """)
        client.send(python=python)
        output = last_tool_text(client)
        for expected in (
            "available module: json",
            "background-thread name: mcp_console_thread_missing",
            "configuring thread",
            "before starting the background thread",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_rejects_automatic_resolution_from_fork_child(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import importlib
            import os
            import select
            import signal

            read_descriptor, write_descriptor = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_descriptor)
                try:
                    importlib.import_module("mcp_console_fork_child_missing")
                except ModuleNotFoundError as error:
                    payload = f"fork-child name: {error.name}\\n{error}"
                else:
                    payload = "missing import unexpectedly succeeded"
                os.write(write_descriptor, payload.encode())
                os._exit(0)

            os.close(write_descriptor)
            chunks = []
            while True:
                readable, _, _ = select.select([read_descriptor], [], [], 10)
                if not readable:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                    raise AssertionError("fork child did not finish its import")
                chunk = os.read(read_descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks).decode()
            os.close(read_descriptor)
            _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            print(payload)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        for expected in (
            "fork-child name: mcp_console_fork_child_missing",
            "main worker process",
            "before",
            "child",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_tool_text(client) == "42\n"
        return client._finish()


def test_times_out_and_polls_automatic_python_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, started, release = checkpoint_uv_environment(
            directory,
            "py-yaml12",
        )
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(binary, ("serve",), environment)
        resolver_released = False
        finished = False
        try:
            client._initialize_and_list_tools()
            # fmt: python
            python = code("""
                automatic_timeout_attempts = (
                    globals().get(
                        "automatic_timeout_attempts",
                        0,
                    )
                    + 1
                )
                import yaml12

                (yaml12.__name__, automatic_timeout_attempts)
                """)
            evaluation = client._start_send(python=python, timeout_ms=1)
            started.wait("automatic Python resolver")
            client._receive(evaluation)
            assert last_tool_text_from_entry(evaluation) == "\n[running]"

            release.release()
            resolver_released = True
            client.send(timeout_ms=30_000)
            assert last_tool_text(client) == "('yaml12', 1)\n"
            transcript = client._finish()
            finished = True
            return transcript
        finally:
            if not resolver_released:
                release.release()
            started.close()
            release.close()
            if not finished:
                stop_client(client)


def test_interrupts_automatic_python_resolver_and_preserves_worker(
    binary: Path,
) -> Transcript:
    requirement = "mcp_console_blocked_automatic_import"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, started, release = checkpoint_uv_environment(
            directory,
            requirement,
        )
        environment.pop("RETICULATE_PYTHON", None)
        environment["RUST_LOG"] = "error"
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            client = McpClient(binary, ("serve",), environment)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        passed = False
        try:
            client._initialize_and_list_tools()
            # fmt: python
            python = code(f"""
                import importlib
                import os

                automatic_interrupt_state = 41
                automatic_interrupt_pid = os.getpid()
                importlib.import_module("{requirement}")
                automatic_interrupt_cell_ran = True
                """)
            evaluation = client._start_send(python=python)
            started.wait("automatic Python resolver")

            interrupt = client._start_session(action="interrupt")
            evaluation_returned = threading.Event()
            forced_release = threading.Event()

            def release_if_evaluation_blocks() -> None:
                if not evaluation_returned.wait(2):
                    forced_release.set()
                    release.release()

            watchdog = threading.Thread(target=release_if_evaluation_blocks)
            watchdog.start()
            client._receive_many([evaluation, interrupt])
            evaluation_returned.set()
            watchdog.join()
            assert not forced_release.is_set(), (
                "interrupt did not stop the automatic Python resolver"
            )
            assert last_tool_text_from_entry(interrupt) == "[interrupt sent]"
            error = last_tool_text_from_entry(evaluation)
            for expected in (
                "ModuleNotFoundError",
                requirement,
                "managed Python resolution",
                "KeyboardInterrupt",
                "requirements.python",
            ):
                assert expected in error, (expected, error)
            evaluation["result"]["content"][0]["text"] = (
                normalize_python_resolution_error(error)
            )

            client.send(
                python=(
                    "automatic_interrupt_state + "
                    "int('automatic_interrupt_cell_ran' not in globals()) + "
                    "int(__import__('os').getpid() == automatic_interrupt_pid) - 1"
                )
            )
            assert last_tool_text(client) == "42\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            release.release()
            started.close()
            release.close()
            if not passed:
                stop_client(client)


def test_restart_discards_unactivated_automatic_python_candidate(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            directory,
            "matplotlib",
        )
        environment.pop("RETICULATE_PYTHON", None)
        environment["TMPDIR"] = temporary
        client = McpClient(binary, ("serve",), environment)
        passed = False
        worker_checkpoints: list[FifoCheckpoint] = []
        try:
            client._initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                invisible(reticulate::py_config())
                activation_ready <- tempfile("mcp-console-activation-ready-")
                activation_release <- tempfile("mcp-console-activation-release-")
                activation_sent <- tempfile("mcp-console-activation-sent-")
                cat(activation_ready, activation_release, activation_sent, sep = "\n")
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 3, setup
            setup["content"][0]["text"] = (
                "<activation ready>\n<activation release>\n<activation sent>"
            )
            activation_ready, activation_release, activation_sent = [
                FifoCheckpoint(Path(path)) for path in paths
            ]
            worker_checkpoints.extend(
                (activation_ready, activation_release, activation_sent)
            )

            # Pause after resolution and immediately before PythonActivated.
            # fmt: r
            r = code(r"""
                globals <- get(".globals", envir = asNamespace("reticulate"))
                original <- activeBindingFunction("python_requirements", globals)
                rm(list = "python_requirements", envir = globals)
                makeActiveBinding("python_requirements", function(value) {
                  if (missing(value)) {
                    return(original())
                  }
                  ready <- fifo(activation_ready, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), ready)
                  close(ready)
                  release <- fifo(activation_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(readBin(release, "raw", n = 1L), charToRaw("1")))
                  close(release)
                  original(value)
                  sent <- fifo(activation_sent, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), sent)
                  close(sent)
                }, globals)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"

            evaluation = client._start_send(python="import yaml12")
            activation_ready.wait("automatic managed Python activation")

            restart = client._start_session(
                action="restart",
                requirements={"python": ["matplotlib"]},
            )
            uv_started.wait("restart Python resolution")
            activation_release.release()
            activation_sent.wait("published automatic Python activation")
            uv_release.release()
            client._receive_many([evaluation, restart])

            evaluation_result = evaluation["result"]
            assert evaluation_result.get("isError") is True, evaluation_result
            assert evaluation_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[stopped by session restart request before evaluation finished]\n"
                        "[worker stopped: in-memory state lost]"
                    ),
                }
            ], evaluation_result
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result

            # fmt: r
            r = code(r"""
                packages <- reticulate::py_require()$packages
                c("matplotlib" %in% packages, "py-yaml12" %in% packages)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[1]  TRUE FALSE\n"
            transcript = client._finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)
            for checkpoint in worker_checkpoints:
                checkpoint.close()
            uv_started.close()
            uv_release.close()


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def last_tool_text_from_entry(entry: dict[str, object]) -> str:
    result = entry["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
