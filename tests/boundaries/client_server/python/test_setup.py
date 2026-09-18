#!/usr/bin/env -S uv run --script

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
def test_calls_python_setup_without_reticulate_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Initialize from R before the first Python cell. Reticulate still
        # activates environments; only Console's helper calls bypass py_eval.
        # fmt: r
        r = code(r"""
            invisible(reticulate::py_config())
            invisible(suppressMessages(base::trace(
              "py_eval",
              tracer = quote(stop("R-mediated Python setup")),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(python="42")
        assert last_result_text(client) == "42\n"
        # fmt: r
        r = code(r"""
            reticulate::py_require(c("matplotlib", "py-yaml12"))
            plt <- reticulate::import("matplotlib.pyplot")
            invisible(plt$show())
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(python="import matplotlib.pyplot as plt; plt.show(); 42")
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_matplotlib_setup_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # A module attribute setter interrupts the first-cell setup itself,
        # after R has initialized the private runtime.
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
                        raise KeyboardInterrupt()
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
        client.send(python="raise AssertionError('interrupted setup ran the cell')")
        assert last_result_text(client) == "\n"
        assert client.transcript[-1]["result"]["isError"] is False
        client.send(python="sys.modules['matplotlib.pyplot'].show(); 42")
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
        client.send(python="raise AssertionError('failed setup ran the cell')")
        result = client.transcript[-1]["result"]
        output = last_result_text(client)
        assert result["isError"] is True, result
        assert output.count("ValueError: matplotlib setup failed") == 1, output
        assert "failed setup ran the cell" not in output, output
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
