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
def test_prepares_without_reticulate_environment_mutators(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="owner_r_state <- 42L")
        client.send(sql="CREATE TABLE owner_state AS SELECT 42 AS answer")
        client.send(
            # fmt: python
            python=code("""
                import os

                owner_object = object()
                owner_identity = id(owner_object)
                owner_pid = os.getpid()
                """)
        )
        client.send(
            # fmt: r
            r=code("""
                namespace <- asNamespace("reticulate")
                mutators <- c(
                  "py_require",
                  "py_reqs_activate",
                  "py_activate_virtualenv",
                  "uv_get_or_create_env"
                )
                originals <- mget(mutators, envir = namespace)
                for (name in mutators) {
                  unlockBinding(name, namespace)
                  assign(
                    name,
                    function(...) stop("reticulate environment mutator reached"),
                    namespace
                  )
                  lockBinding(name, namespace)
                }
                """)
        )
        client.send(
            requirements={"python": ["py-yaml12"]},
            stdin="queued input\n",
            # fmt: python
            python=code("""
                import yaml12

                assert os.getpid() == owner_pid and id(owner_object) == owner_identity
                input()
                """),
        )
        assert last_result_text(client) == "[input requested: \"\"]\n'queued input'\n"
        client.send(python="import humanize; humanize.intcomma(1234)")
        assert last_result_text(client) == "'1,234'\n", last_result_text(client)
        client.send(
            # fmt: r
            r=code("""
                for (name in mutators) {
                  unlockBinding(name, namespace)
                  assign(name, originals[[name]], namespace)
                  lockBinding(name, namespace)
                }
                stopifnot(
                  all(c("py-yaml12", "humanize") %in% reticulate::py_require()$packages),
                  is.null(reticulate::py_require()$python_version),
                  identical(
                    reticulate::py_config()$python,
                    reticulate::import("sys")$executable
                  ),
                  identical(owner_r_state, 42L)
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(sql="SELECT answer FROM owner_state")
        assert last_result_text(client).splitlines()[-1].split() == ["1", "42"]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_materializes_lazy_declarations_without_initializing_python(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                reticulate::py_require("humanize")
                reticulate::py_require("humanize", action = "remove")
                reticulate::py_require("py-yaml12")
                reticulate::py_require(python_version = ">=3.10")
                reticulate::py_require(python_version = ">=3.10", action = "remove")
                reticulate::py_require(exclude_newer = "2026-09-01")
                reticulate::py_require(exclude_newer = NA, action = "set")
                stopifnot(!reticulate::py_available(initialize = FALSE))
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(requirements={"python": ["packaging"]})
        assert last_result_text(client) == "[prepared]", last_result_text(client)
        client.send(r="reticulate::py_available(initialize = FALSE)")
        assert last_result_text(client) == "[1] FALSE\n"
        client.send(control="restart")
        client.send(
            # fmt: r
            r=code("""
                requirements <- reticulate::py_require()
                stopifnot(
                  all(c("py-yaml12", "packaging") %in% requirements$packages),
                  !"humanize" %in% requirements$packages,
                  is.null(requirements$python_version),
                  is.null(requirements$exclude_newer),
                  !reticulate::py_available(initialize = FALSE)
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_with_lazy_exclusion_date(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                reticulate::py_require(exclude_newer = "2026-09-01")
                stopifnot(!reticulate::py_available(initialize = FALSE))
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(python="1 + 1")
        assert last_result_text(client) == "2\n", last_result_text(client)
        client.send(control="restart")
        client.send(
            # fmt: r
            r=code("""
                stopifnot(
                  identical(reticulate::py_require()$exclude_newer, "2026-09-01"),
                  !reticulate::py_available(initialize = FALSE)
                )
                invisible(reticulate::py_config())
                stopifnot(
                  reticulate::py_available(initialize = FALSE),
                  identical(reticulate::py_require()$exclude_newer, "2026-09-01")
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_live_reticulate_requirement_rules(
    binary: Path, execution: Execution
) -> list:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                initial <- reticulate::py_config()
                current <- reticulate::py_require()
                replacement <- tryCatch(
                  reticulate::py_require(c(current$packages, "py-yaml12"), action = "set"),
                  error = conditionMessage
                )
                stopifnot(
                  identical(
                    replacement,
                    "After Python has initialized, only `action = 'add'` is supported."
                  ),
                  identical(reticulate::py_require(), current)
                )
                reticulate::py_require(python_version = ">=3.10")
                stopifnot(identical(
                  reticulate::py_require()$python_version,
                  current$python_version
                ))
                reticulate::py_require("py-yaml12")
                config <- reticulate::py_config()
                sys <- reticulate::import("sys")
                stopifnot(
                  "py-yaml12" %in% reticulate::py_require()$packages,
                  identical(config$python, sys$executable),
                  identical(config$executable, sys$executable),
                  identical(config$prefix, sys$prefix),
                  identical(config$exec_prefix, sys$exec_prefix),
                  identical(config$virtualenv, sys$prefix),
                  !grepl(initial$prefix, config$pythonpath, fixed = TRUE),
                  !nzchar(config$virtualenv_activate) ||
                    identical(dirname(config$virtualenv_activate), dirname(sys$executable)),
                  identical(config$pythonhome, paste(sys$prefix, sys$exec_prefix, sep = ":"))
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
