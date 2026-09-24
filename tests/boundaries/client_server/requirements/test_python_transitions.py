#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import recording_uv_environment, uv_tool_run_requirements
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_owns_managed_python_transitions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Instrument the old planner, then exercise only public declarations,
        # explicit preparation, and automatic imports through the console.
        # fmt: r
        r = code(r"""
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "py_reqs_plan",
              tracer = quote(stop("reticulate calculated the transition")),
              print = FALSE,
              where = namespace
            )))
            reticulate::py_require(c("numpy", "unused", "numpy"), action = "set")
            reticulate::py_require("unused", action = "remove")
            reticulate::py_require(python_version = ">=3.10, <4")
            reticulate::py_require(exclude_newer = "2026-01-01")
            reticulate::py_require(exclude_newer = NA_character_, action = "remove")
            declared <- reticulate::py_require()
            stopifnot(
              identical(declared$packages, "numpy"),
              identical(declared$python_version, c(">=3.10", "<4")),
              identical(declared["exclude_newer"], list(exclude_newer = NULL)),
              !reticulate::py_available(initialize = FALSE)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(requirements={"python": ["numpy"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(r="stopifnot(!reticulate::py_available(initialize = FALSE))")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "'yaml12'\n"
        ), last_tool_text(client)
        # fmt: r
        r = code(r"""
            current <- reticulate::py_require()
            stopifnot(
              identical(current$packages, c("py-yaml12", "numpy")),
              identical(current$python_version, declared$python_version)
            )
            reticulate::py_require("py-yaml12")
            reticulate::py_require("absent", action = "remove")
            reticulate::py_require(rev(current$packages), action = "set")
            reticulate::py_require(python_version = ">=3.10")
            unchanged <- reticulate::py_require()
            stopifnot(
              identical(unchanged$packages, current$packages),
              identical(unchanged$python_version, declared$python_version),
              length(unchanged$history) == length(current$history) + 4L
            )
            for (action in c("remove", "set")) {
              message <- tryCatch(
                reticulate::py_require("numpy", action = action),
                error = conditionMessage
              )
              stopifnot(identical(
                message,
                "After Python has initialized, only `action = 'add'` is supported."
              ))
            }
            stopifnot(identical(reticulate::py_require(), unchanged))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(requirements={"python": ["packaging"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(python="import packaging; packaging.__name__")
        assert last_tool_text(client) == "'packaging'\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_preparation_restoration_and_live_noops(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory, fail_requirement="py-yaml12"
        )
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                reticulate::py_require(
                  "numpy",
                  python_version = ">=3.10, <4",
                  action = "set"
                )
                before <- reticulate::py_require()
                stopifnot(!reticulate::py_available(initialize = FALSE))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            failed = client.send(requirements={"python": ["py-yaml12"]})
            assert failed["isError"] is True, failed
            error = failed["content"][0]["text"]
            assert "synthetic uv failure" in error, error
            # fmt: r
            r = code(r"""
                stopifnot(
                  identical(reticulate::py_require(), before),
                  !reticulate::py_available(initialize = FALSE)
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            Path(environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).unlink()
            client.send(python="None")
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            resolutions = uv_tool_run_requirements(record)
            # fmt: r
            r = code(r"""
                before <- reticulate::py_require()
                reticulate::py_require("numpy")
                reticulate::py_require("absent", action = "remove")
                reticulate::py_require(before$packages, action = "set")
                reticulate::py_require(python_version = ">=3.10")
                unchanged <- reticulate::py_require()
                stopifnot(
                  identical(unchanged$packages, before$packages),
                  identical(unchanged$python_version, c(">=3.10", "<4")),
                  length(unchanged$history) == length(before$history) + 4L
                )
                message <- tryCatch(
                  reticulate::py_require("numpy==0"),
                  error = conditionMessage
                )
                stopifnot(identical(
                  message,
                  paste(
                    "After Python has initialized, only `action = 'add'` with new packages is supported.",
                    "You tried to add `numpy==0` but requirements contain `numpy` already."
                  )
                ))
                message <- tryCatch(
                  reticulate::py_require(exclude_newer = "2026-01-01"),
                  error = conditionMessage
                )
                stopifnot(identical(
                  message,
                  "`exclude_newer` cannot be changed after Python has initialized."
                ))
                version <- reticulate::py_eval(
                  "'.'.join(str(x) for x in __import__('sys').version_info[:3])"
                )
                message <- tryCatch(
                  reticulate::py_require(python_version = "<3"),
                  error = conditionMessage
                )
                stopifnot(
                  identical(
                    message,
                    paste0(
                      "Python version requirements cannot be changed after Python has been initialized.\n",
                      "* Python version request: '<3'\n",
                      "* Python version initialized: '",
                      version,
                      "'"
                    )
                  ),
                  identical(reticulate::py_require(), unchanged)
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            assert uv_tool_run_requirements(record) == resolutions
            client.send(requirements={"python": ["py-yaml12"]})
            assert last_tool_text(client) == "[prepared]", last_tool_text(client)
            # fmt: r
            r = code(r"""
                stopifnot(
                  identical(reticulate::py_require()$python_version, c(">=3.10", "<4")),
                  identical(reticulate::py_require()$packages, c("py-yaml12", "numpy"))
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_incompatible_live_libpython_before_activation(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="import sys; initial_prefix = sys.prefix")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # fmt: r
        r = code(r"""
            before <- reticulate::py_require()
            namespace <- asNamespace("reticulate")
            original <- get("python_config", namespace)
            replacement <- function(...) {
              config <- original(...)
              config$libpython <- "incompatible-libpython"
              config
            }
            unlockBinding("python_config", namespace)
            assign("python_config", replacement, envir = namespace)
            lockBinding("python_config", namespace)
            outcome <- tryCatch(
              reticulate::py_require("py-yaml12"),
              error = conditionMessage
            )
            stopifnot(
              grepl(
                "New environment does not use the same Python binary",
                outcome,
                fixed = TRUE
              ),
              grepl("new libpython: incompatible-libpython", outcome, fixed = TRUE),
              identical(reticulate::py_require(), before)
            )
            unlockBinding("python_config", namespace)
            assign("python_config", original, envir = namespace)
            lockBinding("python_config", namespace)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(python="sys.prefix == initial_prefix")
        assert last_tool_text(client) == "True\n", last_tool_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_activation_python_failure_once_and_restores_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="import runpy; original_run_path = runpy.run_path")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(r="before <- reticulate::py_require()")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # fmt: python
        python = code("""
            def fail_activation_hook(_path):
                raise ValueError("activation hook failed")


            runpy.run_path = fail_activation_hook
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(r='reticulate::py_require("py-yaml12")')
        output = last_tool_text(client)
        assert output.count("ValueError: activation hook failed") == 1, output
        client.send(r="stopifnot(identical(reticulate::py_require(), before))")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(python="runpy.run_path = original_run_path")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(r='reticulate::py_require("py-yaml12")')
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == "'yaml12'\n", last_tool_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_activation_interrupt_conditions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="None")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # An R interrupt during candidate configuration must still reach the
        # caller's handler rather than becoming an ordinary preparation error.
        # fmt: r
        r = code(r"""
            before <- reticulate::py_require()
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "python_config",
              tracer = quote(stop(structure(
                list(message = "activation interrupted", call = NULL),
                class = c("interrupt", "condition")
              ))),
              print = FALSE,
              where = namespace
            )))
            outcome <- tryCatch(
              reticulate::py_require("py-yaml12"),
              interrupt = function(condition) conditionMessage(condition),
              error = function(condition) paste("error:", conditionMessage(condition))
            )
            stopifnot(
              identical(outcome, "activation interrupted"),
              identical(reticulate::py_require(), before)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
