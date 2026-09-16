#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_traceback_paths
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_python_preserves_exact_queued_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="input()", stdin="caf\u00e9\0tail\n")
        assert (
            last_result_text(client)
            == "[input requested: \"\"]\n'caf\u00e9\\x00tail'\n"
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_r_activation_does_not_initialize_python_or_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code("""
            stopifnot(!any(c("reticulate", "duckdb", "DBI") %in% loadedNamespaces()))
            answer <- 41L
            answer + 1L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n"
        client.send(python="r.answer + 1")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_input_without_losing_state(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            answer = 41
            input("ready> ")
            """)
        client.send(python=python)
        assert (
            last_result_text(client)
            == '[input requested: "ready> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client)
        client.send(python="answer + 1")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_routes_interrupts_across_nested_languages(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="answer = 41")
        for cell in (
            {"r": 'readline("R> ")'},
            {"python": 'r.readline("nested R> ")'},
            {"r": "reticulate::py_eval(\"input('nested Python> ')\")"},
        ):
            client.send(**cell)
            assert "[waiting for stdin]" in last_result_text(client)
            result = client.send(control="interrupt")
            result["content"][0]["text"] = normalize_python_traceback_paths(
                last_result_text(client)
            )
            client.send(python="answer + 1")
            assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_defers_python_interrupts_until_r_resumes(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: python
            python=code("""
                import signal


                def finite_python():
                    signal.raise_signal(signal.SIGINT)
                    print("Python completed")
                    return 42
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(
            # fmt: r
            r=code(r"""
                tryCatch(
                  {
                    suspendInterrupts({
                      answer <- reticulate::py_eval("finite_python()")
                      cat("R received:", answer, "\n")
                    })
                    # Check the deferred R interrupt while the handler is active.
                    Sys.sleep(0)
                  },
                  interrupt = function(condition) cat("R interrupt delivered\n")
                )
                """)
        )
        assert last_result_text(client) == (
            "Python completed\nR received: 42 \nR interrupt delivered\n"
        ), last_result_text(client)
        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_python_requirements_preserve_live_objects_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"python": ["packaging"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import importlib.metadata
            import os
            import packaging

            original_pid = os.getpid()
            original_object = object()
            original_identity = id(original_object)
            packaging_version = importlib.metadata.version("packaging")
            packaging_version
            """)
        client.send(python=python)
        version = last_result_text(client).strip().strip("'")
        assert version != "23.2", version
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<loaded packaging version>\n"
        )
        client.send(stdin="queued\n")
        result = client.send(requirements={"python": ["packaging==23.2"]})
        error = result["content"][0]["text"]
        assert result["isError"] is True, result
        assert f"Cannot replace loaded packaging {version} with 23.2" in error, error
        result["content"][0]["text"] = error.replace(
            version, "<loaded packaging version>"
        )
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            import yaml12
            import defusedxml

            assert os.getpid() == original_pid
            assert id(original_object) == original_identity
            assert importlib.metadata.version("packaging") == packaging_version
            input()
            """)
        client.send(python=python)
        assert last_result_text(client).endswith("[input requested: \"\"]\n'queued'\n")
        client.send(control="restart")
        # fmt: python
        python = code("""
            import yaml12, defusedxml

            "original_object" in globals()
            """)
        client.send(python=python)
        assert last_result_text(client) == "False\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reticulate_tracks_console_python_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="invisible(reticulate::py_config())")
        assert last_result_text(client) == "[done]"
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: r
        r = code("""
            stopifnot(
              identical(
                reticulate::py_config()$python,
                reticulate::import("sys")$executable
              ),
              "py-yaml12" %in% reticulate::py_require()$packages
            )
            py$answer <- 42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(python="answer")
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
