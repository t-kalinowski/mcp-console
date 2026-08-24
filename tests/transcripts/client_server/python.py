#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


def python_version_constraint_error(constraint: str) -> str:
    return (
        f"Python version constraint `{constraint}` is not accepted: host-side managed "
        "resolution accepts version numbers and supported PEP 440 version specifiers only"
    )


def normalize_duckdb_resolution_error(error: str, extension: str) -> str:
    detail = next(
        line.strip().removeprefix("! ")
        for line in error.splitlines()
        if f'Failed to download extension "{extension}"' in line
    )
    return detail.partition(' at URL "')[0]


def ir_cache_directory(environment: dict[str, str]) -> str:
    ir = shutil.which("ir", path=environment.get("PATH"))
    assert ir is not None, "ir is required"
    cache = subprocess.run(
        [ir, "cache", "dir"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()
    assert cache and Path(cache).is_absolute(), (
        f"ir returned invalid cache directory: {cache}"
    )
    return cache


def matplotlib_test_environment(cache_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    cache = ir_cache_directory(environment)
    environment["IR_CACHE_DIR"] = cache
    environment["XDG_CACHE_HOME"] = str(cache_home)
    assert ir_cache_directory(environment) == cache
    return environment


def test_preserves_configured_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        external_python_worker <- Sys.getpid()
        external_python_state <- 42L
        stopifnot(
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        "configured-by-user"
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "configured-by-user"\n'
    disabled = (
        "managed Python requirements are disabled because the session uses a "
        "user-selected Python environment"
    )
    for action in ("prepare", "restart"):
        client.session(
            action=action,
            requirements={"python": ["numpy"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert last_tool_text(client) == disabled

    client.send(
        r="external_python_combined_side_effect <- TRUE",
        requirements={"python": ["numpy"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert last_tool_text(client) == disabled
    # Reach the same worker-originated resolver request used by reticulate's
    # managed hooks. The server must enforce the external-selection policy
    # even though those hooks are not installed for this worker.
    # fmt: r
    r = code(r"""
        environment_request <- jsonlite::toJSON(list(
          requirements = list(packages = I("numpy")),
          retained_requirements = list(packages = I("numpy"))
        ), auto_unbox = TRUE)
        environment_error <- tryCatch(
          .Call("mcp_console_resolve_python", environment_request),
          error = conditionMessage
        )
        version_request <- jsonlite::toJSON(list(
          constraints = I(">=3.11")
        ), auto_unbox = TRUE)
        version_error <- tryCatch(
          .Call("mcp_console_resolve_python_version", version_request),
          error = conditionMessage
        )
        cat(environment_error, version_error, sep = "\n")
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output == f"{disabled}\n{disabled}\n", repr(output)
    # fmt: r
    r = code(r"""
        stopifnot(
          identical(Sys.getpid(), external_python_worker),
          identical(external_python_state, 42L),
          !exists("external_python_combined_side_effect", inherits = FALSE),
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        42L
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client._finish()


def test_preserves_empty_python_environment(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[1] "managed"\n'
    return client._finish()


def test_rejects_python_older_than_3_10(binary: Path) -> Transcript:
    interpreter = Path("/usr/bin/python3")
    version = subprocess.run(
        (interpreter, "-c", "import sys; print(sys.version_info[:2])"),
        check=True,
        capture_output=True,
        text=True,
    )
    assert version.stdout.strip() == "(3, 9)", version.stdout

    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = str(interpreter)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    bridge_failure = "Python bridge failed during R evaluation\n"
    version_failure = (
        "Error: MCP Console requires Python 3.10 or later; "
        "selected interpreter reports Python 3.9\n"
    )
    worker_failure = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 1]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )
    output = result["content"][0]["text"]
    expected = {
        bridge_failure + version_failure + worker_failure,
        version_failure + bridge_failure + worker_failure,
    }
    assert output in expected, output
    result["content"][0]["text"] = bridge_failure + version_failure + worker_failure
    return client._finish()


def managed_python_transcript(binary: Path, configured: bool) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        config <- reticulate::py_config()
        history <- reticulate::py_require()$history
        stopifnot(
          identical(python, "managed"),
          file.exists(config$python),
          isTRUE(config$ephemeral),
          "pandas" %in% reticulate::py_require()$packages,
          !any(vapply(
            history,
            function(request) identical(request$requested_from, "base"),
            logical(1L)
          ))
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    python = code("""
        import io
        import pandas as pd

        frame = pd.read_csv(io.StringIO("value\\n40\\n2\\n"))
        int(frame["value"].sum())
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "42\n", repr(output)
    return client._finish()


def test_evaluates_with_default_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=False)


def test_evaluates_with_explicit_managed_python(binary: Path) -> Transcript:
    return managed_python_transcript(binary, configured=True)


def test_runs_joblib_process_backend(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(
        python=python,
        requirements={"python": ["joblib"]},
    )
    output = last_tool_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client._finish()


def test_runs_joblib_process_backend_after_live_resolution(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.send(python="import sys")
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client._finish()


def test_inspects_sandbox_child_processes_with_psutil(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import subprocess
        import sys

        import psutil

        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        child = subprocess.Popen(command)
        try:
            descendants = psutil.Process().children(recursive=True)
            observed = [process.pid for process in descendants]
        finally:
            child.terminate()
            child.wait()

        child.pid in observed
        """)
    client.send(
        python=python,
        requirements={"python": ["psutil"]},
    )
    output = last_tool_text(client)
    assert output == "True\n", repr(output)
    return client._finish()


def test_sends_python_cell_with_initial_requirements(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import yaml12

        print(yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "yaml12\n"
    return client._finish()


def test_uses_200_column_default(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import shutil

        import numpy as np
        import pandas as pd

        print(f"terminal columns: {shutil.get_terminal_size().columns}")
        print(f"pandas display.width: {pd.get_option('display.width')}")
        print(f"NumPy linewidth: {np.get_printoptions()['linewidth']}")
        pd.DataFrame(
            [range(12)],
            columns=[f"column_{column:02}" for column in range(12)],
        )
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output.startswith(
        "terminal columns: 200\npandas display.width: 200\nNumPy linewidth: 200\n"
    ), repr(output)
    for column in range(12):
        assert f"column_{column:02}" in output
    assert "..." not in output
    assert "[1 rows x 12 columns]" not in output
    return client._finish()


def test_uses_200_column_default_after_r_initializes_python(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        reticulate::py_run_string(
          "
        import numpy as np
        import pandas as pd
        "
        )
        cat(
          "R-first NumPy linewidth: ",
          reticulate::py_eval("np.get_printoptions()['linewidth']"),
          "\nR-first pandas display.width: ",
          reticulate::py_eval("pd.get_option('display.width')"),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "R-first NumPy linewidth: 200\nR-first pandas display.width: 200\n"
    )
    return client._finish()


def test_prints_requirements_with_host_uv_cache(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        trusted_cache = temporary / "trusted-uv-cache"
        worker_cache = temporary / "worker-uv-cache"
        uv_record = temporary / "uv-environment.jsonl"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_WORKER_UV_CACHE"] = str(worker_cache)
        environment["UV_CACHE_DIR"] = str(trusted_cache)
        environment["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
        environment["UV_OFFLINE"] = "1"
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        uv_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(r"""
            worker_cache <- Sys.getenv("MCP_CONSOLE_TEST_WORKER_UV_CACHE")
            Sys.setenv(
              UV_CACHE_DIR = worker_cache,
              UV_DEFAULT_INDEX = "file:///worker-selected-index"
            )
            reticulate::py_require("py-yaml12")
            invisible(reticulate::py_config())
            stopifnot(
              reticulate::py_module_available("yaml12"),
              identical(Sys.getenv("UV_CACHE_DIR"), worker_cache),
              identical(
                Sys.getenv("UV_DEFAULT_INDEX"),
                "file:///worker-selected-index"
              )
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", client.transcript[-1]
        records = [
            json.loads(line)
            for line in uv_record.read_text(encoding="utf-8").splitlines()
        ]
        assert records, "runtime managed resolution did not invoke uv"
        expected = {
            "UV_CACHE_DIR": str(trusted_cache),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_OFFLINE": None,
        }
        assert all(record == expected for record in records), records
        assert not worker_cache.exists(), worker_cache
        return client._finish()


def test_uses_current_r_library_for_managed_python_resolution(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        uv_record = temporary / "uv-environment.jsonl"
        r_libs_record = temporary / "uv-r-libs.jsonl"
        environment, _ = r_test_environment()
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_R_LIBS_RECORD"] = str(r_libs_record)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        client.send(r="initial_r_library <- .libPaths()[[1L]]")
        assert last_tool_text(client) == "[done]"

        def current_r_library() -> str:
            # fmt: r
            r = code(r"""
                cat(jsonlite::toJSON(.libPaths()[[1L]], auto_unbox = TRUE))
                """)
            client.send(r=r)
            output = last_tool_text(client)
            library = json.loads(output)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                '"<current managed R library>"'
            )
            return library

        def assert_resolver_used(library: str) -> None:
            records = [
                json.loads(line)
                for line in r_libs_record.read_text(encoding="utf-8").splitlines()
            ]
            assert records, "managed Python resolution did not invoke uv"
            assert all(record is not None for record in records), records
            first_libraries = [record.split(os.pathsep, 1)[0] for record in records]
            assert first_libraries == [library] * len(records), first_libraries

        client.session(action="prepare", requirements={"r": ["zeallot"]})
        assert last_tool_text(client) == "[prepared]"
        prepared_r_library = current_r_library()
        uv_record.write_text("", encoding="utf-8")
        r_libs_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", client.transcript[-1]
        assert_resolver_used(prepared_r_library)

        uv_record.write_text("", encoding="utf-8")
        r_libs_record.write_text("", encoding="utf-8")
        client.session(
            action="restart",
            requirements={"r": ["praise"], "python": ["six"]},
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        restarted_r_library = current_r_library()
        assert_resolver_used(restarted_r_library)
        return client._finish()


def test_validates_registry_only_python_requirements(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        uv_record = temporary / "uv-environment.jsonl"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        environment = os.environ.copy()
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        uv_record.write_text("", encoding="utf-8")

        project = temporary / "project"
        archive = temporary / "package.whl"
        duckdb_extension = "not_a_real_duckdb_extension"
        rejected = [
            str(project),
            "./project",
            "../project",
            project.as_uri(),
            "-e ./project",
            "example @ https://example.invalid/example.whl",
            str(archive),
            "./package.whl",
            "package.whl",
            "package.tar.gz",
        ]
        for index, requirement in enumerate(rejected):
            action = "prepare" if index % 2 == 0 else "restart"
            client.session(
                action=action,
                requirements={
                    "r": ["praise"],
                    "python": [requirement],
                    "duckdb": [duckdb_extension],
                },
            )
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert last_tool_text(client) == named_requirement_error(requirement)
        assert uv_record.read_text(encoding="utf-8") == ""

        client.session(
            action="prepare",
            requirements={"duckdb": [duckdb_extension]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        result["content"][0]["text"] = normalize_duckdb_resolution_error(
            result["content"][0]["text"], duckdb_extension
        )

        prepared = "requests[socks]>=2,<3; python_version < '0'"
        client.session(
            action="prepare",
            requirements={"python": [prepared]},
        )
        assert last_tool_text(client) == "[prepared]"
        assert uv_record.read_text(encoding="utf-8") != ""
        restarted = "urllib3!=2.0.0; python_version < '0'"
        client.session(
            action="restart",
            requirements={"python": [restarted]},
        )
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        # fmt: r
        r = code(rf"""
            requirements <- reticulate::py_require()$packages
            stopifnot(
              "{prepared}" %in% requirements,
              "{restarted}" %in% requirements,
              !any(c(
                {", ".join(json.dumps(requirement) for requirement in rejected)}
              ) %in% requirements)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", client.transcript[-1]

        worker_executable = temporary / "worker-python"
        worker_retained_selector = temporary / "worker-retained-python"
        worker_installation = temporary / "worker-python-installation"
        for selector in (worker_executable, worker_retained_selector):
            selector.write_text(
                '#!/bin/sh\ntouch "$0.executed"\nexit 97\n',
                encoding="utf-8",
            )
            selector.chmod(0o755)
        worker_installation.mkdir()
        accepted_packages = [
            "requests",
            "requests[socks]",
            "requests>=2,<3",
            "requests[socks]>=2; python_version >= '3.10'",
        ]
        accepted_version_constraints = [
            "3.11",
            "3.14.0a3",
            ">=3.9",
            ">=3.9,<3.13",
            "==3.12.*",
        ]
        rejected_version_constraints = [
            "",
            "./python",
            "../python",
            "file:///tmp/python",
            "python3",
            "~=3.11",
            "===3.11",
        ]
        uv_record.write_text("", encoding="utf-8")
        # Reach both worker-originated resolver requests directly. Interpreter
        # selectors must be rejected before either host resolver invokes uv.
        # fmt: r
        r = code(rf"""
            selector_worker_pid <- Sys.getpid()
            selector_sentinel <- 42L
            packages <- reticulate::py_require()$packages
            accepted_package_request <- jsonlite::toJSON(list(
              requirements = list(
                packages = I(c({", ".join(json.dumps(package) for package in accepted_packages)})),
                python_version = I("python3")
              ),
              retained_requirements = list(
                packages = I(c({", ".join(json.dumps(package) for package in accepted_packages)})),
                python_version = I(">=3.10")
              )
            ), auto_unbox = TRUE)
            accepted_package_error <- tryCatch(
              .Call("mcp_console_resolve_python", accepted_package_request),
              error = conditionMessage
            )
            environment_request <- jsonlite::toJSON(list(
              requirements = list(
                packages = I(packages),
                python_version = I({json.dumps(str(worker_executable))})
              ),
              retained_requirements = list(
                packages = I(packages),
                python_version = I(">=3.10")
              )
            ), auto_unbox = TRUE)
            environment_error <- tryCatch(
              .Call("mcp_console_resolve_python", environment_request),
              error = conditionMessage
            )
            retained_environment_request <- jsonlite::toJSON(list(
              requirements = list(
                packages = I(packages),
                python_version = I(">=3.10")
              ),
              retained_requirements = list(
                packages = I(packages),
                python_version = I({json.dumps(str(worker_retained_selector))})
              )
            ), auto_unbox = TRUE)
            retained_environment_error <- tryCatch(
              .Call("mcp_console_resolve_python", retained_environment_request),
              error = conditionMessage
            )
            accepted_version_request <- jsonlite::toJSON(list(
              constraints = I(c(
                {", ".join(json.dumps(constraint) for constraint in accepted_version_constraints)},
                {json.dumps(str(worker_installation))}
              ))
            ), auto_unbox = TRUE)
            accepted_version_error <- tryCatch(
              .Call(
                "mcp_console_resolve_python_version",
                accepted_version_request
              ),
              error = conditionMessage
            )
            rejected_version_errors <- vapply(
              c({", ".join(json.dumps(constraint) for constraint in rejected_version_constraints)}),
              function(constraint) {{
                request <- jsonlite::toJSON(list(
                  constraints = I(constraint)
                ), auto_unbox = TRUE)
                tryCatch(
                  .Call("mcp_console_resolve_python_version", request),
                  error = conditionMessage
                )
              }},
              character(1L),
              USE.NAMES = FALSE
            )
            cat(
              accepted_package_error,
              environment_error,
              retained_environment_error,
              accepted_version_error,
              rejected_version_errors,
              sep = "\n"
            )
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == (
            python_version_constraint_error("python3")
            + "\n"
            + python_version_constraint_error(str(worker_executable))
            + "\n"
            + python_version_constraint_error(str(worker_retained_selector))
            + "\n"
            + python_version_constraint_error(str(worker_installation))
            + "\n"
            + "\n".join(
                python_version_constraint_error(constraint)
                for constraint in rejected_version_constraints
            )
            + "\n"
        ), repr(output)
        assert uv_record.read_text(encoding="utf-8") == ""
        assert not Path(f"{worker_executable}.executed").exists()
        assert not Path(f"{worker_retained_selector}.executed").exists()
        # fmt: r
        r = code(r"""
            identical(Sys.getpid(), selector_worker_pid) &&
              identical(selector_sentinel, 42L) &&
              is.null(reticulate::py_require()$python_version)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] TRUE\n"

        runtime_rejected = "./runtime-project"
        uv_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(rf"""
            reticulate::py_require({
              json.dumps(runtime_rejected)
            })
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        error = named_requirement_error(runtime_rejected)
        assert error in result["content"][0]["text"], result
        result["content"][0]["text"] = error
        assert uv_record.read_text(encoding="utf-8") == ""
        transcript = client._finish()
        transcript_json = json.dumps(transcript)
        transcript_json = transcript_json.replace(
            str(project), "<absolute project path>"
        ).replace(str(archive), "<absolute archive path>")
        transcript_json = transcript_json.replace(
            str(worker_installation), "<worker installation path>"
        ).replace(str(worker_retained_selector), "<worker retained selector>")
        transcript_json = transcript_json.replace(
            str(worker_executable), "<worker executable path>"
        )
        return json.loads(transcript_json)


def test_recovers_from_python_version_resolution_failure(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        uv = Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        failure_marker = temporary / "fail-version-resolution"
        environment = os.environ.copy()
        environment["RETICULATE_UV"] = str(uv)
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(temporary / "uv.jsonl")
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = "list"

        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        failure_marker.touch()
        # fmt: r
        r = code(r"""
            worker_pid <- Sys.getpid()
            print(reticulate::py_require())
            """)
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert "managed Python version resolution failed" in output
        assert "synthetic uv failure" in output
        normalized = output.replace(str(uv), "<uv executable>")
        result["content"][0]["text"] = (
            "\n".join(line.rstrip() for line in normalized.splitlines()) + "\n"
        )

        client.send(r="identical(Sys.getpid(), worker_pid)")
        assert last_tool_text(client) == "[1] TRUE\n"
        return client._finish()


def test_prepares_initial_python_requirements(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    invalid = "not a valid requirement !!!"

    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    recorded_error = named_requirement_error(invalid)
    assert result["content"][0]["text"] == recorded_error
    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == recorded_error
    client.session(
        action="prepare",
        requirements={"python": ["numpy\npandas"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error("numpy\npandas")
    # fmt: r
    r = code(r"""
        seed <- tail(reticulate::py_require()$history, 1L)[[1L]]
        printed_requirements <- capture.output(print(reticulate::py_require()))
        stopifnot(
          identical(seed$requested_from, "mcp-console"),
          identical(seed$action, "set"),
          isFALSE(seed$exclude_newer_supplied),
          identical(seed$packages, c("numpy", "pandas", "py-yaml12")),
          length(printed_requirements) > 0L
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    return client._finish()


def test_prepares_explicit_numpy_requirement(binary: Path) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"python": ["numpy"]},
    )
    assert last_tool_text(client) == "[prepared]"
    # fmt: r
    r = code(r"""
        stopifnot(Sys.getenv("RETICULATE_PYTHON") == "managed")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    return client._finish()


def test_does_not_fail_resolution_when_matplotlib_cache_cannot_be_written(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = matplotlib_test_environment(temporary / "host-cache")
        cache_directory = temporary / "user-matplotlib"
        environment["MPLCONFIGDIR"] = str(cache_directory)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=temporary,
        )
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        caches = list(cache_directory.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        caches[0].unlink()
        caches[0].mkdir()

        client.session(
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        assert caches[0].is_dir()
        assert not [
            path for path in cache_directory.glob("fontlist-v*.json") if path.is_file()
        ]
        client.send(
            python="(__import__('matplotlib').__name__, __import__('yaml12').__name__)"
        )
        assert last_tool_text(client) == "('matplotlib', 'yaml12')\n"
        return client._finish()


def test_restart_loses_state_and_retains_python_requirements(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"

    client.session(action="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )

    # fmt: python
    python = code("""
        import yaml12

        "restart_marker" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client._finish()


def test_restart_discards_pre_marker_python_activation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required for managed-Python tests"
        uv = Path(__file__).parents[2] / "fixtures" / "checkpoint_uv"
        uv_started = FifoCheckpoint(temporary / "uv-started")
        uv_release = FifoCheckpoint(temporary / "uv-release")

        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["RETICULATE_UV"] = str(uv)
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_ARGUMENT"] = "matplotlib"
        environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_CLAIM"] = str(
            temporary / "uv-claimed"
        )
        environment["MCP_CONSOLE_TEST_UV_STARTED"] = str(uv_started.path)
        environment["MCP_CONSOLE_TEST_UV_RELEASE"] = str(uv_release.path)

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

            # Pause the real managed worker after its new environment resolves,
            # immediately before its active binding publishes python_activated.
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
                reticulate::py_require("py-yaml12")
                """)
            evaluation = client._start_send(r=r)
            activation_ready.wait("managed Python activation")

            restart = client._start_session(
                action="restart",
                requirements={"python": ["matplotlib"]},
            )
            uv_started.wait("restart Python resolution")
            activation_release.release()
            activation_sent.wait("published managed Python activation")
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

            # The replacement environment wins over the old generation's
            # activation, even though that event preceded ordered retirement.
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


def test_prepares_python_requirements_after_worker_startup(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    python = code("""
        import importlib.util; import os; import sys
        sentinel = 42; worker_pid = os.getpid(); initial_prefix = sys.prefix
        importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "True\n"

    invalid = "not a valid requirement !!!"
    client.session(
        action="prepare",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    python = code("""
        sentinel, os.getpid() == worker_pid, importlib.util.find_spec("yaml12") is None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, True, True)\n"

    python = code("""
        import os; import sys; import yaml12
        (sentinel, os.getpid() == worker_pid, sys.prefix != initial_prefix, yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"

    client.session(action="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(r="is.null(reticulate::py_require()$python_version)")
    assert last_tool_text(client) == "[1] TRUE\n"
    # fmt: python
    python = code("""
        import yaml12

        "sentinel" in globals(), yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(False, 'yaml12')\n"
    return client._finish()


def test_failed_live_python_requirements_do_not_run_cell(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(python="import os; live_sentinel = 42; live_worker_pid = os.getpid()")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        reticulate_namespace <- asNamespace("reticulate")
        original_py_require <- get("py_require", envir = reticulate_namespace)
        unlockBinding("py_require", reticulate_namespace)
        assign(
          "py_require",
          function(...) stop("synthetic live Python preparation failure"),
          envir = reticulate_namespace
        )
        lockBinding("py_require", reticulate_namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    result = client.send(
        python="failed_live_python_cell = True",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "synthetic live Python preparation failure"
    ), result

    # fmt: r
    r = code(r"""
        unlockBinding("py_require", reticulate_namespace)
        assign(
          "py_require",
          original_py_require,
          envir = reticulate_namespace
        )
        lockBinding("py_require", reticulate_namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    python = code("""
        import os
        import yaml12

        (
            live_sentinel,
            os.getpid() == live_worker_pid,
            "failed_live_python_cell" not in globals(),
            yaml12.__name__,
        )
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "(42, True, True, 'yaml12')\n"
    return client._finish()


def test_prepares_after_idle_python_resolution(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="prepare", requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          reticulate::py_config()
          cat("idle Python ready\n")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle Python callback")

    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.send(r="reticulate::py_require()$packages")
    assert "idle Python ready\n" in last_tool_text(client)
    assert '"py-yaml12"' in last_tool_text(client)
    return client._finish()


def test_retains_idle_python_activation_during_continuous_collection(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.session(action="prepare", requirements={"r": ["later"]})
    client.send(r="invisible(reticulate::py_config())")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        callback_complete <- tempfile("mcp-console-callback-complete-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          reticulate::py_require("py-yaml12")
          cat("idle Python activated\n")
          stopifnot(file.create(callback_complete))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, callback_complete, sep = "\n")
        """)
    client.send(r=r)
    (callback_complete,) = release_worker_callback_gate(
        client,
        "idle Python activation",
        ("complete",),
    )
    deadline = time.monotonic() + 30
    while not callback_complete.exists():
        assert client.process.poll() is None, (
            "mcp-console stopped before idle Python activation completed"
        )
        if time.monotonic() >= deadline:
            raise AssertionError("idle Python activation did not complete")
        time.sleep(0.01)
    wait_for_idle_output(
        client,
        "idle Python activated\n\n[idle]",
        "idle Python activation output",
    )

    client.session(action="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


def test_does_not_retain_stale_python_materialization(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(r="invisible(reticulate::py_config())")
    assert last_tool_text(client) == "[done]"

    # Make explicit preparation resolve the unchanged environment before its
    # real activation. The first candidate is materialized but never activated.
    # fmt: r
    r = code(r"""
        namespace <- asNamespace("reticulate")
        original_py_require <- get("py_require", envir = namespace)
        injected <- FALSE
        replacement <- function(...) {
          if (!injected) {
            injected <<- TRUE
            requirements <- original_py_require()
            invisible(get("uv_get_or_create_env", envir = namespace)(
              requirements$packages,
              requirements$python_version,
              requirements$exclude_newer
            ))
          }
          original_py_require(...)
        }
        unlockBinding("py_require", namespace)
        assign("py_require", replacement, envir = namespace)
        lockBinding("py_require", namespace)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    client.session(
        action="prepare",
        requirements={"python": ["py-yaml12"]},
    )
    assert last_tool_text(client) == "[prepared]"
    client.session(action="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(python="import yaml12; yaml12.__name__")
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


def test_failed_restart_requirements_preserve_worker(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    client.send(python="restart_marker = 42")
    assert last_tool_text(client) == "[done]"
    invalid = "not a valid requirement !!!"

    client.session(
        action="restart",
        requirements={"python": [invalid]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid)

    client.send(python="restart_marker")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_layers_python_requirements_declared_by_r_packages(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[2] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        # fmt: python
        python = code("""
            import importlib.util
            import sys

            runtime_marker = 42
            initial_prefix = sys.prefix
            importlib.util.find_spec("yaml12") is None
            """)
        client.send(python=python)
        assert last_tool_text(client) == "True\n"

        # fmt: r
        r = code(r"""
            initial_libpython <- reticulate::py_config()$libpython
            initial_worker <- Sys.getpid()
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        client.send(r="library(mcpconsolepyrequire)")
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            identical(reticulate::py_config()$libpython, initial_libpython) &&
              identical(Sys.getpid(), initial_worker)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[1] TRUE\n"

        # fmt: python
        python = code("""
            import yaml12

            (runtime_marker, yaml12.__name__, sys.prefix != initial_prefix)
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(42, 'yaml12', True)\n", repr(output)

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        # fmt: python
        python = code("""
            import yaml12

            ("runtime_marker" in globals(), yaml12.__name__)
            """)
        client.send(python=python)
        assert last_tool_text(client) == "(False, 'yaml12')\n"

        client.session(
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_tool_text(client) == "[prepared]"
        return client._finish()


def test_does_not_retain_package_requirements_before_python_initializes(
    binary: Path,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).parents[2] / "fixtures" / "py_require"
    with tempfile.TemporaryDirectory() as library:
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (library, environment.get("R_LIBS")))
        )
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            library(mcpconsolepyrequire)
            request <- tail(reticulate::py_require()$history, 1L)[[1L]]
            stopifnot(
              identical(request$requested_from, "mcpconsolepyrequire"),
              isTRUE(request$env_is_package)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"

        # A lazy declaration is worker-owned until Python initializes or an
        # explicit preparation materializes it.
        # fmt: r
        r = code(r"""
            tools::pskill(Sys.getpid(), signal = 9L)
            """).removesuffix("\n")
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        actual = result["content"][0]["text"]
        assert actual == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), repr(actual)

        # fmt: r
        r = code(r"""
            "py-yaml12" %in% reticulate::py_require()$packages
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[1] FALSE\n", repr(output)
        return client._finish()


def test_retains_python_activation_before_later_cell_failure(
    binary: Path,
) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(reticulate::py_config())
        invisible(reticulate::py_require("py-yaml12"))
        stopifnot(reticulate::py_module_available("yaml12"))
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker terminated by signal 9]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    # The successful activation is retained even though the cell later kills
    # the worker before its ordinary completion message.
    # fmt: r
    r = code(r"""
        worker_pid <- Sys.getpid()
        "py-yaml12" %in% reticulate::py_require()$packages
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output == "[1] TRUE\n", repr(output)

    # fmt: python
    python = code("""
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python)
    assert last_tool_text(client) == "'yaml12'\n"
    return client._finish()


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
            Path(__file__).parents[2] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        python = code("""
            runtime_generation_marker = "original runtime retained"
            import time; from pathlib import Path
            temporary = Path(__import__("os").environ["TMPDIR"])
            (temporary / "python-evaluation-running").touch()
            while not (temporary / "release-python").exists():
                time.sleep(0.01)
            """)
        client.send(python=python, timeout_ms=0)
        assert last_tool_text(client) == "\n[running; poll with an empty send]"
        client.transcript[-1]["result"]["content"][0]["text"] = "<running>"
        running = wait_for_worker_file(
            Path(temporary_directory),
            "python-evaluation-running",
            client,
        )
        release = running.parent / "release-python"
        uv_record.write_text("", encoding="utf-8")

        session_returned = threading.Event()
        forced_release = threading.Event()

        def release_blocked_evaluation() -> None:
            if not session_returned.wait(2):
                forced_release.set()
                release.touch()

        watchdog = threading.Thread(target=release_blocked_evaluation)
        watchdog.start()
        client.session(
            action="prepare",
            requirements={"python": ["py-yaml12"]},
        )
        session_returned.set()
        watchdog.join()
        assert not forced_release.is_set(), "session waited for the running evaluation"
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

        release.touch()
        client.send()
        assert last_tool_text(client) == "[done]"
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
                    repeat {}
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

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
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
                from pathlib import Path

                inspect.getmro.__code__ = inspect._mcp_original_getmro_code
                python_interrupt_state = 41
                Path(
                    os.environ["TMPDIR"],
                    "python-interrupt-started",
                ).touch()
                while True:
                    pass
                """)
            client.send(python=python, timeout_ms=0)
            assert last_tool_text(client) == "\n[running; poll with an empty send]"
            wait_for_worker_file(
                temporary_path,
                "python-interrupt-started",
                client,
            )

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
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
                      repeat {}
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

            client.session(action="interrupt")
            assert last_tool_text(client) == "[interrupt sent]"
            client.send(timeout_ms=3_000)
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
            client.send(r="resolver_interrupt_state <- 41L")
            assert last_tool_text(client) == "[done]"

            preparation = client._start_send(
                r="resolver_interrupt_cell_ran <- TRUE",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            interrupt = client._start_session(action="interrupt")
            preparation_returned = threading.Event()
            forced_release = threading.Event()

            def release_if_preparation_blocks() -> None:
                if not preparation_returned.wait(2):
                    forced_release.set()
                    uv_release.release()

            watchdog = threading.Thread(target=release_if_preparation_blocks)
            watchdog.start()
            client._receive_many([preparation, interrupt])
            preparation_returned.set()
            watchdog.join()
            assert not forced_release.is_set(), (
                "interrupt did not stop the Python resolver"
            )
            assert interrupt["result"] == {
                "content": [{"type": "text", "text": "[interrupt sent]"}],
                "isError": False,
            }, interrupt
            assert preparation["result"]["isError"] is True, preparation
            error = preparation["result"]["content"][0]["text"]
            assert "managed Python resolution" in error, error
            preparation["result"]["content"][0]["text"] = (
                "managed Python resolution cancelled by interrupt"
            )

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
            uv_release.release()
            uv_started.close()
            uv_release.close()
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
            second_prepare = client._start_session(
                action="prepare",
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

            restart = client._start_session(action="restart")
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
        client.session(
            action="prepare",
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


def test_evaluates_cells_in_persistent_reticulate_state(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        from_r <- 40L
        python_source_visible <- function() {
          calls <- vapply(sys.calls(), deparse1, character(1))
          marker <- paste0("unique_python_", "source_marker")
          any(grepl(marker, calls, fixed = TRUE))
        }
        reticulate::py_run_string(
          r"---(
        test_sys = __import__("sys")
        test_types = __import__("types")
        __import__ = None
        exec = None
        setattr = None
        _io = "user io"
        _main = "user main"
        _sys = "user sys"
        sorted = "user sorted"
        test_sys.modules["matplotlib.pyplot"] = test_types.SimpleNamespace(
            get_fignums=lambda: [],
            close=lambda *_args, **_kwargs: None,
        )
        )---"
        )
        """)
    client.send(r=r)
    # fmt: python
    python = code("""
        answer = r.from_r + 1
        print("from Python")
        (
            answer + 1,
            (__import__, exec, setattr) == (None, None, None),
            (_io, _main, _sys, sorted) == ("user io", "user main", "user sys", "user sorted"),
            "_mcp_console" not in globals()
            and test_sys.modules["_mcp_console"].__name__ == "_mcp_console",
        )
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "from Python\n(42, True, True, True)\n", repr(output)
    # fmt: python
    python = code("""
        1
        2
        """)
    client.send(python=python)
    assert last_tool_text(client) == "2\n"
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    # fmt: r
    r = code(r"""
        stopifnot(!"package:reticulate" %in% search())
        py <- "user shadow"
        stopifnot(identical(py, "user shadow"))
        rm(py)
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        unique_python_source_marker = r.python_source_visible()
        unique_python_source_marker
        """)
    client.send(python=python)
    output = last_tool_text(client)
    assert output == "False\n", repr(output)
    # fmt: r
    r = code(r"""
        .mcp_console_private <- "user value"
        .mcp_console_python_source <- "user source"
        .mcp_console_python_filename <- "user filename"
        is.null <- function(...) FALSE
        """)
    client.send(r=r)
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        compile = "user compile"
        eval = "user eval"
        exec = "user exec"
        isinstance = "user isinstance"
        BaseException = "user BaseException"
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="answer + 1")
    assert last_tool_text(client) == "42\n"
    # fmt: python
    python = code("""
        import builtins as test_builtins

        test_original_import = test_builtins.__import__
        test_builtins.__import__ = None
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    # fmt: python
    python = code("""
        test_builtins.__import__ = test_original_import
        answer + 1
        """)
    client.send(python=python)
    assert last_tool_text(client) == "42\n"
    client.send(python="silent = True")
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        rm(list = ls())
        py$assigned_from_r <- 43L
        py$answer
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[1] 41\n"
    # fmt: python
    python = code("""
        assigned_from_r
        """)
    client.send(python=python)
    assert last_tool_text(client) == "43\n"
    return client._finish()


def test_returns_r_plots_from_python_bridge(binary: Path) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, ("serve",), environment)
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        bridge_plot <- function() {
          plot(1:3)
          invisible(NULL)
        }
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    expected_plot = reference_plots(
        rscript,
        environment,
        r + "bridge_plot()\n",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    # fmt: python
    python = code("""
        print("before plot")
        r.bridge_plot()
        print("after plot")
        """)
    client.send(python=python)
    assert_result_content(
        client,
        ["before plot\nafter plot\n", expected_plot[0]],
    )
    return client._finish()


def test_returns_matplotlib_plots(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        workspace = temporary / "workspace-one"
        workspace.mkdir()
        system_profiler = shutil.which("system_profiler")
        assert system_profiler is not None, "system_profiler is required"
        path = os.environ.get("PATH")
        assert path is not None, "PATH is required"
        probe = temporary / "bin" / "system_profiler"
        probe.parent.mkdir()
        probe.write_text(
            code(r"""
                #!/bin/sh
                : > "$TMPDIR/mcp-console-font-discovery"
                exec "$MCP_CONSOLE_TEST_SYSTEM_PROFILER" "$@"
                """),
            encoding="utf-8",
        )
        probe.chmod(0o755)
        fontconfig = temporary / "fonts.conf"
        fontconfig.write_text(
            code(r"""
                <?xml version="1.0"?>
                <!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
                <fontconfig>
                  <cachedir prefix="xdg">mcp-console-test</cachedir>
                </fontconfig>
                """),
            encoding="utf-8",
        )
        host_matplotlib = temporary / "host-matplotlib"
        host_matplotlib.mkdir()
        host_matplotlibrc = host_matplotlib / "matplotlibrc"
        host_matplotlibrc.write_text("lines.linewidth: 7.25\n", encoding="utf-8")
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["FONTCONFIG_FILE"] = str(fontconfig)
        environment["MPLCONFIGDIR"] = str(host_matplotlib)
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(host_matplotlibrc)
        environment["MCP_CONSOLE_TEST_SYSTEM_PROFILER"] = system_profiler
        environment["PATH"] = os.pathsep.join((str(probe.parent), path))
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPL_IGNORE_SYSTEM_FONTS", None)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            reticulate::py_require("matplotlib")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        host_discovery = temporary / "mcp-console-font-discovery"
        assert host_discovery.is_file()
        persistent_caches = list(host_matplotlib.glob("fontlist-v*.json"))
        assert len(persistent_caches) == 1, persistent_caches
        persistent_cache_bytes = persistent_caches[0].read_bytes()
        host_discovery.unlink()

        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib
            import matplotlib.pyplot as plt

            assert (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve()
            )
            assert matplotlib.rcParams["lines.linewidth"] == 7.25

            later_figure, later_axes = plt.subplots(num=20)
            later_axes.plot([1, 2, 3], [1, 2, 1])
            later_reference = Path(os.environ["TMPDIR"]) / "matplotlib-later-reference.png"
            later_figure.savefig(later_reference, format="png")

            figure, axes = plt.subplots(num=10)
            axes.plot([1, 2, 3], [3, 1, 2])
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache.write_text(
                '{"__class__":"FontManager","_version":999}',
                encoding="utf-8",
            )

            reference = Path(os.environ["TMPDIR"]) / "matplotlib-reference.png"
            figure.savefig(reference, format="png")
            """)
        client.send(python=python)
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-reference.png",
            client,
        )
        later_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-later-reference.png",
            client,
        )
        assert_result_content(
            client,
            [reference.read_bytes(), later_reference.read_bytes()],
            image_reference="live matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            shown_figure, shown_axes = plt.subplots()
            shown_axes.plot([1, 2, 3], [1, 3, 2])
            shown_reference = Path(os.environ["TMPDIR"]) / "matplotlib-shown-reference.png"
            shown_figure.savefig(shown_reference, format="png")
            print("before show")
            plt.show()
            print("after show")
            shown_figure
            """)
        client.send(python=python)
        shown_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-shown-reference.png",
            client,
        )
        result = client.transcript[-1]["result"]
        output = result["content"][0]["text"]
        assert output.startswith("before show\nafter show\n<Figure size "), output
        assert output.endswith(" with 1 Axes>\n"), output
        result["content"][0]["text"] = (
            "before show\nafter show\n<matplotlib figure displayhook representation>\n"
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], shown_reference.read_bytes()],
            image_reference="live shown matplotlib savefig {page}",
        )

        # fmt: python
        python = code("""
            closed_figure, closed_axes = plt.subplots()
            closed_axes.plot([1, 2, 3], [2, 1, 3])
            closed_reference = Path(os.environ["TMPDIR"]) / "matplotlib-closed-reference.png"
            closed_figure.savefig(closed_reference, format="png")
            plt.close(closed_figure)
            plt.get_fignums()
            """)
        client.send(python=python)
        closed_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-closed-reference.png",
            client,
        )
        assert closed_reference.is_file()
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            axes.plot([1, 3], [2, 0])
            plt.get_fignums()
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            error_figure, error_axes = plt.subplots()
            error_axes.plot([1, 2], [2, 1])
            error_reference = Path(os.environ["TMPDIR"]) / "matplotlib-error-reference.png"
            error_figure.savefig(error_reference, format="png")
            raise ValueError("cell failed")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("ValueError: cell failed\n"), output
        error_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-error-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], error_reference.read_bytes()],
            image_reference="live error-cell matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

        # fmt: python
        python = code("""
            def fail_plot_capture(*args, **kwargs):
                raise RuntimeError("plot render failed")


            failed_figure = plt.figure()
            failed_figure.savefig = fail_plot_capture
            figure, axes = plt.subplots()
            axes.plot([1, 3], [2, 0])
            second_reference = Path(os.environ["TMPDIR"]) / "matplotlib-second-reference.png"
            figure.savefig(second_reference, format="png")
            """)
        client.send(python=python)
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        output = result["content"][0]["text"]
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.endswith("RuntimeError: plot render failed\n"), output
        second_reference = wait_for_worker_file(
            Path(temporary_directory),
            "matplotlib-second-reference.png",
            client,
        )
        assert_result_content(
            client,
            [result["content"][0]["text"], second_reference.read_bytes()],
            image_reference="live second matplotlib savefig {page}",
        )

        client.send(python="plt.get_fignums()")
        assert last_tool_text(client) == "[]\n"

        # Replacing the private link must not make a later runtime resolution
        # overwrite user-owned worker state or discard the worker.
        # fmt: python
        python = code("""
            private_cache = next(
                path
                for path in Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json")
                if path.is_symlink()
            )
            private_cache_bytes = private_cache.read_bytes()
            private_cache.unlink()
            private_cache.write_bytes(private_cache_bytes)
            cache_link_replaced = True
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]"

        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        client.send(python="(cache_link_replaced, __import__('yaml12').__name__)")
        assert last_tool_text(client) == "(True, 'yaml12')\n"

        client.session(action="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            marker = Path(os.environ["TMPDIR"]) / "mcp-console-font-discovery"
            invalid_cache = Path(os.environ["MPLCONFIGDIR"]) / "fontlist-v999.json"
            invalid_cache_was_seeded = invalid_cache.exists()

            import matplotlib
            import matplotlib.font_manager

            config = Path(matplotlib.matplotlib_fname())
            font_cache = next(Path(os.environ["MPLCONFIGDIR"]).glob("fontlist-v*.json"))
            try:
                with font_cache.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                font_cache_read_only = True
            else:
                font_cache_read_only = False

            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            try:
                config.with_name("worker-payload").write_text("payload", encoding="utf-8")
            except PermissionError:
                config_directory_read_only = True
            else:
                config_directory_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                font_cache_read_only,
                config_read_only,
                config_directory_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
                marker.exists(),
                invalid_cache_was_seeded,
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 7.25, True, True, True, True, False, False)\n", repr(
            output
        )
        assert not list(temporary.rglob("mcp-console-font-discovery"))
        transcript = client._finish()
        assert (
            host_matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 7.25\n"
        )
        assert not (host_matplotlib / "worker-payload").exists()
        assert len(persistent_caches) == 1, persistent_caches
        assert persistent_caches[0].read_bytes() == persistent_cache_bytes
        assert not (persistent_caches[0].parent / "fontlist-v999.json").exists()
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_explicit_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        explicit = temporary / "explicit"
        explicit.mkdir()
        explicit_rc = explicit / "matplotlibrc"
        explicit_rc.write_text("lines.linewidth: 8.25\n", encoding="utf-8")
        inherited = temporary / "inherited"
        inherited.mkdir()
        (inherited / "matplotlibrc").write_text(
            "lines.linewidth: 18.25\n",
            encoding="utf-8",
        )
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["TMPDIR"] = temporary_directory
        environment["MPLCONFIGDIR"] = str(inherited)
        environment["MATPLOTLIBRC"] = str(explicit_rc)
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(explicit_rc)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            config = Path(matplotlib.matplotlib_fname())
            try:
                with config.open("a", encoding="utf-8"):
                    pass
            except PermissionError:
                config_read_only = True
            else:
                config_read_only = False

            private_probe = Path(os.environ["MPLCONFIGDIR"]) / "config-write-probe"
            private_probe.write_text("ok", encoding="utf-8")

            (
                config.resolve() == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
                config_read_only,
                private_probe.read_text(encoding="utf-8") == "ok",
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 8.25, True, True)\n", repr(output)
        transcript = client._finish()
        assert explicit_rc.read_text(encoding="utf-8") == "lines.linewidth: 8.25\n"
        assert not list(explicit.glob("fontlist-v*.json"))
        caches = list(inherited.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_inherits_default_matplotlib_config(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        home = temporary / "home"
        matplotlib = home / ".matplotlib"
        matplotlib.mkdir(parents=True)
        matplotlibrc = matplotlib / "matplotlibrc"
        matplotlibrc.write_text("lines.linewidth: 9.25\n", encoding="utf-8")
        r_environment, rscript = r_test_environment()
        # fmt: r
        source = code(r"""
            writeLines(.libPaths())
            """)
        r_libraries = subprocess.run(
            [rscript, "--vanilla", "-e", source],
            check=True,
            capture_output=True,
            text=True,
            env=r_environment,
        ).stdout.splitlines()
        uv = shutil.which("uv")
        assert uv is not None, "real uv is required for managed-Python tests"
        uv_cache = subprocess.run(
            [uv, "cache", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        uv_python = subprocess.run(
            [uv, "python", "dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        environment = matplotlib_test_environment(temporary / "host-cache")
        environment["HOME"] = str(home)
        environment["TMPDIR"] = temporary_directory
        environment["R_LIBS_USER"] = os.pathsep.join(r_libraries)
        environment["RETICULATE_UV"] = uv
        environment["UV_CACHE_DIR"] = uv_cache
        environment["UV_PYTHON_INSTALL_DIR"] = uv_python
        environment["MPL_IGNORE_SYSTEM_FONTS"] = "1"
        environment["MCP_CONSOLE_TEST_MATPLOTLIBRC"] = str(matplotlibrc)
        environment.pop("MATPLOTLIBRC", None)
        environment.pop("MPLCONFIGDIR", None)
        client = McpClient(binary, ("serve",), environment)
        client._initialize_and_list_tools()
        client.session(
            action="prepare",
            requirements={"python": ["matplotlib"]},
        )
        assert last_tool_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import os
            from pathlib import Path

            import matplotlib

            (
                Path(matplotlib.matplotlib_fname()).resolve()
                == Path(os.environ["MCP_CONSOLE_TEST_MATPLOTLIBRC"]).resolve(),
                matplotlib.rcParams["lines.linewidth"],
            )
            """)
        client.send(python=python)
        output = last_tool_text(client)
        assert output == "(True, 9.25)\n", repr(output)
        transcript = client._finish()
        assert matplotlibrc.read_text(encoding="utf-8") == "lines.linewidth: 9.25\n"
        caches = list(matplotlib.glob("fontlist-v*.json"))
        assert len(caches) == 1, caches
        assert not list(
            (temporary / "host-cache" / "mcp-console" / "matplotlib").glob(
                "fontlist-v*.json"
            )
        )
        return transcript


def test_runs_async_python_explicitly(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        import asyncio


        async def answer():
            await asyncio.sleep(0)
            return 42
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(python="asyncio.run(answer())")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_recovers_from_python_errors(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: python
    python = code("""
        answer = 41


        def fail():
            raise ValueError("boom")


        fail()
        """)
    client.send(python=python)
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
    client.send(python=python)
    output = last_tool_text(client)
    assert output.startswith("Traceback (most recent call last):\n")
    assert "<mcp-console:python:" in output
    assert output.endswith("SyntaxError: 'await' outside function\n")
    client.send(python='"compile_partial" in globals()')
    assert last_tool_text(client) == "False\n"

    client.send(python="nul_state = 42\0")
    output = last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    assert "SyntaxError" in output
    assert "null bytes" in output
    client.send(python="answer")
    assert last_tool_text(client) == "41\n"
    return client._finish()


def test_routes_python_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        name = input("name> ")
        name
        """)
    client.send(python=python)
    assert last_tool_text(client) == '[input requested: "name> "]\n[waiting for stdin]'
    client.send(stdin="Ada\n")
    assert last_tool_text(client) == "'Ada'\n"

    # fmt: python
    python = code("""
        color = input("color> ")
        color
        """)
    client.send(python=python, stdin="blue\n")
    assert last_tool_text(client) == ("[input requested: \"color> \"]\n'blue'\n")

    # fmt: python
    python = code("""
        import sys

        direct = sys.stdin.readline()
        direct
        """)
    client.send(python=python, stdin="fd 0\n")
    assert last_tool_text(client) == "'fd 0\\n'\n"
    return client._finish()


def test_python_debugger_input(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()

    # fmt: python
    python = code("""
        import pdb

        debug_value = 41
        pdb.set_trace()
        debug_value += 1
        """)
    client.send(python=python, stdin="p debug_value\n")
    output = last_tool_text(client)
    assert output.count('[input requested: "(Pdb) "]') == 2, output
    assert output.endswith('41\n[input requested: "(Pdb) "]\n[waiting for stdin]'), (
        output
    )

    wait_for_evaluation_output(
        client,
        "[done]",
        "Python debugger input",
        stdin="continue\n",
        timeout_ms=3_000,
    )
    client.send(python="debug_value")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def test_restarts_after_python_bridge_failure(binary: Path) -> Transcript:
    client = McpClient(binary, ("serve",))
    client._initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python_worker_marker <- TRUE
        Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
        invisible(suppressMessages(base::trace(
          "py_discover_config",
          tracer = quote(base::signalCondition(base::structure(
            base::list(message = "synthetic interrupt", call = NULL),
            class = c("interrupt", "condition")
          ))),
          print = FALSE,
          where = asNamespace("reticulate")
        )))
        """)
    client.send(r=r)
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    bridge_failure = "Python bridge failed during R evaluation\n"
    python_failure = (
        "Error in py_discover_config(required_module, use_environment) : \n"
        "  Python specified in RETICULATE_PYTHON "
        "(/mcp-console-missing-python) does not exist\n"
    )
    worker_failure = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 1]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )
    output = result["content"][0]["text"]
    expected = {
        bridge_failure + python_failure + worker_failure,
        python_failure + bridge_failure + worker_failure,
    }
    assert output in expected, output
    result["content"][0]["text"] = bridge_failure + python_failure + worker_failure
    client.send(r='exists("python_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(python="6 * 7")
    assert last_tool_text(client) == "42\n"
    return client._finish()


def last_tool_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


if __name__ == "__main__":
    run_this_suite(__file__)
