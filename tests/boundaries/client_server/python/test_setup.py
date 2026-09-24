#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_exact_interleaving,
    last_result_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import send_and_collect_runtime_python_resolution
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_activates_without_reticulate_virtualenv_helper(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            invisible(reticulate::py_config())
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "py_activate_virtualenv",
              tracer = quote(stop("reticulate activation helper was called")),
              print = FALSE,
              where = namespace
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            initial_prefix = sys.prefix
            live_object = object()
            live_object_id = id(live_object)
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            config <- reticulate::py_config()
            sys <- reticulate::import("sys", convert = FALSE)
            stopifnot(
              identical(config$executable, reticulate::py_to_r(sys$executable)),
              identical(config$prefix, reticulate::py_to_r(sys$prefix)),
              isTRUE(config$available),
              isTRUE(config$ephemeral)
            )
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess
            import yaml12

            assert id(live_object) == live_object_id
            assert sys.executable != initial_executable
            assert sys.prefix != initial_prefix
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_setup_after_r_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                invisible(reticulate::py_config())
                """)
        )
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # Load multiprocessing before activation so a spawned child must use
        # the updated interpreter after the new requirements are available.
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require(c("matplotlib", "py-yaml12"))
            plt <- reticulate::import("matplotlib.pyplot")
            invisible(plt$show())
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess

            import matplotlib.pyplot as plt

            assert sys.executable != initial_executable
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            plt.show()
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_matplotlib_setup_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # A module attribute setter blocks first-cell setup on managed input.
        # Its public input request is the checkpoint for a real interrupt.
        # fmt: r
        r = code(r"""
            reticulate::py_run_string(r"---(
            import sys
            import types

            class InterruptingPyplot(types.ModuleType):
                interrupted = False

                def __setattr__(self, name, value):
                    if name == "show" and not self.interrupted:
                        self.interrupted = True
                        input("Matplotlib setup> ")
                    super().__setattr__(name, value)

                def get_fignums(self):
                    return []

                def close(self, *args):
                    pass

            sys.modules["matplotlib.pyplot"] = InterruptingPyplot("matplotlib.pyplot")
            )---")
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted setup ran the cell")
                """)
        )
        assert last_result_text(client) == (
            '[input requested: "Matplotlib setup> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "\n",
            "Matplotlib setup interruption",
            control="interrupt",
        )
        client.send(
            # fmt: python
            python=code("""
                sys.modules["matplotlib.pyplot"].show()
                42
                """)
        )
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_matplotlib_setup_error_once(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            reticulate::py_run_string(
              r"---(
            import sys

            class FailingPyplot:
                def __setattr__(self, name: str, value: object) -> None:
                    raise ValueError("matplotlib setup failed")

            sys.modules["matplotlib.pyplot"] = FailingPyplot()
            )---"
            )
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("failed setup ran the cell")
                """)
        )
        result = client.transcript[-1]["result"]
        output = last_result_text(client)
        assert result["isError"] is True, result
        setup_failure = (
            "Error in py_eval_impl(code, convert) : \n"
            "  ValueError: matplotlib setup failed\n"
            "Run `reticulate::py_last_error()` for details.\n"
        )
        bridge_failure = "Python bridge failed during R evaluation\n"
        worker_failure = (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 1]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        assert output.endswith(worker_failure), output
        # R diagnostics and terminal worker stderr use independent transports.
        # Check every byte and each stream's order before canonicalizing them.
        assert_exact_interleaving(
            output.removesuffix(worker_failure), setup_failure, bridge_failure
        )
        result["content"][0]["text"] = setup_failure + bridge_failure + worker_failure
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
