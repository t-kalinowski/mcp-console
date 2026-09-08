#!/usr/bin/env -S uv run --script


import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend(binary: Path, execution: Execution) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(
        python=python,
        requirements={"python": ["joblib"]},
    )
    output = last_result_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_joblib_process_backend_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(python="import sys")
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        from joblib import Parallel, delayed

        Parallel(n_jobs=2)(delayed(abs)(value) for value in range(-2, 3))
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "[2, 1, 0, 1, 2]\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_runs_spawn_process_after_live_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import multiprocessing.spawn
        import sys

        initial_executable = sys.executable
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        import multiprocessing
        import sys

        import joblib

        context = multiprocessing.get_context("spawn")
        with context.Pool(1) as pool:
            child_executable = pool.apply(
                eval,
                ("__import__('sys').executable",),
            )

        (
            initial_executable != sys.executable,
            child_executable == sys.executable,
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "(True, True)\n", repr(output)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
