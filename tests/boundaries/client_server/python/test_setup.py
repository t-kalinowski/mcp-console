#!/usr/bin/env -S uv run --script

import json
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_exact_interleaving,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.processes import process_exists, stop_process_id
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_resolution_error
from support.native import build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.resolvers import send_and_collect_runtime_python_resolution
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_cancels_native_inspection_and_retries(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", temporary / "venv"],
            check=True,
            capture_output=True,
        )
        selected = temporary / "venv/bin/python"
        fixture = Path(__file__).resolve().parents[3] / "fixtures"
        site = Path(
            subprocess.check_output(
                [selected, fixture / "native_python_paths.py", "site-packages"],
                text=True,
            ).strip()
        )
        shutil.copyfile(
            fixture / "native_python_sitecustomize.py", site / "sitecustomize.py"
        )
        (site / "inspection-mode").write_text("inspection-checkpoint")
        ready = FifoCheckpoint.create(site / "inspection-ready")
        release = FifoCheckpoint.create(site / "inspection-release")
        pid = None
        try:
            serve = (
                execution.serve("--writable-root", temporary_directory)
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, serve) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: r
                    r=code(f"""
                        retained_value <- 41L
                        retained_pid <- Sys.getpid()
                        Sys.setenv(RETICULATE_PYTHON = {
                          json.dumps(str(selected))
                        })
                        """)
                )
                operation = client.start_send(
                    # fmt: python
                    python=code("""
                        unexecuted_value = 1
                        """)
                )
                ready.wait("native Python inspection")
                pid = int((site / "inspection-pid").read_text())
                result_file = Path((site / "inspection-result").read_text())
                assert result_file.exists()
                interrupt = client.start_send(control="interrupt", timeout_ms=0)
                client.receive_many([operation, interrupt])
                assert not process_exists(pid), "inspection child was not reaped"
                pid = None
                assert not result_file.exists(), "inspection output was not removed"
                (site / "inspection-mode").write_text("inspection-output")
                client.send(
                    # fmt: r
                    r=code("""
                        stopifnot(Sys.getpid() == retained_pid)
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        retained_value + 1L
                        """)
                )
                assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        retried_value = 43
                        retried_value
                        """)
                )
                assert last_result_text(client) == "43\n", client.transcript[-1]
                assert (site / "inspection-count").read_text() == "1\n1\n"
                client.send(
                    # fmt: python
                    python=code("""
                        retried_value + 1
                        """)
                )
                assert last_result_text(client) == "44\n", client.transcript[-1]
                assert (site / "inspection-count").read_text() == "1\n1\n"
                records = client.finish()
                for record in records:
                    if "send" in record and "r" in record["send"]:
                        record["send"]["r"] = record["send"]["r"].replace(
                            str(selected), "<selected python>"
                        )
                return records
        finally:
            stop_process_id(pid)
            ready.close()
            release.close()


@executions(DIRECT, SANDBOXED)
def test_retries_failed_native_inspection(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                retained_pid <- Sys.getpid()
                retained_value <- 41L
                invisible(asNamespace("reticulate"))
                return_missing <- TRUE
                assignInNamespace(
                  "py_discover_config",
                  local({
                    original <- get("py_discover_config", asNamespace("reticulate"))
                    function(...) {
                      config <- original(...)
                      if (return_missing) {
                        config$python <- "/missing-selected-python"
                      }
                      config
                    }
                  }),
                  "reticulate"
                )
                startup_environment <- Sys.getenv(
                  c(
                    "VIRTUAL_ENV",
                    "R_SESSION_INITIALIZED",
                    "PYTHONIOENCODING",
                    "PATH",
                    "LD_LIBRARY_PATH",
                    "PYTHONPATH"
                  ),
                  unset = NA_character_
                )
                """)
        )
        client.send(
            # fmt: python
            python=code("""
                unexecuted_value = 1
                """)
        )
        assert last_result_text(client) == (
            "Error: selected Python executable is not an absolute file: /missing-selected-python\n"
        ), client.transcript[-1]
        client.send(
            # fmt: r
            r=code("""
                stopifnot(Sys.getpid() == retained_pid)
                stopifnot(!reticulate::py_available(initialize = FALSE))
                stopifnot(identical(
                  Sys.getenv(names(startup_environment), unset = NA_character_),
                  startup_environment
                ))
                failure <- tryCatch(reticulate::py_config(), error = identity)
                stopifnot(inherits(failure, "console_python_inspection_error"))
                stopifnot(!reticulate::py_available(initialize = FALSE))
                return_missing <- FALSE
                retained_value + 1L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                retried_value = 43
                retried_value
                """)
        )
        assert last_result_text(client) == "43\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_console_configures_selected_python(
    binary: Path, execution: Execution
) -> Transcript:
    transcript = []
    for first in ("python", "r"):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--without-pip",
                    "--copies",
                    temporary / "venv",
                ],
                check=True,
                capture_output=True,
            )
            selected = temporary / "venv/bin/python"
            serve = (
                execution.serve("--writable-root", temporary_directory)
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, serve) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: r
                    r=code(f"""
                        selected_python <- {json.dumps(str(selected))}
                        Sys.unsetenv("RETICULATE_PYTHON")
                        reticulate::use_python(selected_python, required = TRUE)
                        selection_calls <- 0L
                        assignInNamespace("py_discover_config", local({{
                          original <- get("py_discover_config", asNamespace("reticulate"))
                          function(...) {{
                            selection_calls <<- selection_calls + 1L
                            config <- original(...)
                            stopifnot(identical(normalizePath(config$python), normalizePath(selected_python)))
                            selected_python <<- config$python
                            config$libpython <- "/missing-reticulate-libpython"
                            config$pythonhome <- "/missing-reticulate-home"
                            config
                          }}
                        }}), "reticulate")
                        """)
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                if first == "python":
                    client.send(
                        # fmt: python
                        python=code("""
                            owned_value = 41
                            """)
                    )
                else:
                    client.send(
                        # fmt: r
                        r=code("""
                            reticulate::py_run_string("owned_value = 41")
                            """)
                    )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(
                    # fmt: r
                    r=code("""
                        config <- reticulate::py_config()
                        stopifnot(selection_calls == 1L)
                        stopifnot(identical(config$python, selected_python))
                        stopifnot(file.exists(config$libpython))
                        stopifnot(dir.exists(config$pythonhome))
                        stopifnot(!identical(config$pythonhome, dirname(dirname(selected_python))))
                        reticulate::py_to_r(reticulate::py$owned_value) + 1L
                        """)
                )
                assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        owned_value + 2
                        """)
                )
                assert last_result_text(client) == "43\n", client.transcript[-1]
                records = client.finish()
                for record in records:
                    if "send" in record and "r" in record["send"]:
                        record["send"]["r"] = record["send"]["r"].replace(
                            str(selected), "<selected python>"
                        )
                transcript.extend(records)
    return transcript


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_python_first_initializes_before_reticulate_attaches(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(binary, serve) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                startup_calls <- 0L
                callback_calls <- 0L
                Sys.unsetenv("RETICULATE_PYTHON")
                expected_python <- normalizePath(Sys.which("python3"))
                options(reticulate.python.beforeInitialized = function() {{
                  callback_calls <<- callback_calls + 1L
                  initialized <- .C(
                    getNativeSymbolInfo(
                      "mcp_console_probe_python_initialized",
                      PACKAGE = .GlobalEnv$startup_probe
                    ),
                    value = 0L
                  )$value
                  if (initialized == 1L) {{
                    stop(sprintf("callback %d ran after CPython initialization", callback_calls))
                  }}
                  reticulate::use_python(expected_python, required = TRUE)
                }})
                invisible(suppressMessages(base::trace(
                  "py_initialize",
                  tracer = quote({{
                    assign(
                      "startup_calls",
                      get("startup_calls", envir = .GlobalEnv) + 1L,
                      envir = .GlobalEnv
                    )
                    stopifnot(identical(
                      .C(
                        getNativeSymbolInfo(
                          "mcp_console_probe_python_initialized",
                          PACKAGE = .GlobalEnv$startup_probe
                        ),
                        value = 0L
                      )$value,
                      1L
                    ))
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(
                # fmt: python
                python=code("""
                    startup_value = 41
                    startup_value + 1
                    """)
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            client.send(
                # fmt: r
                r=code("""
                    stopifnot(startup_calls == 1L)
                    stopifnot(callback_calls == 1L)
                    stopifnot(identical(
                      normalizePath(reticulate::py_config()$python),
                      expected_python
                    ))
                    reticulate::py_to_r(reticulate::py$startup_value) + 1L
                    """)
            )
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_r_first_initializes_before_reticulate_attaches(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(binary, serve) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                startup_calls <- 0L
                invisible(suppressMessages(base::trace(
                  "py_initialize",
                  tracer = quote({{
                    assign(
                      "startup_calls",
                      get("startup_calls", envir = .GlobalEnv) + 1L,
                      envir = .GlobalEnv
                    )
                    stopifnot(identical(
                      .C(
                        getNativeSymbolInfo(
                          "mcp_console_probe_python_initialized",
                          PACKAGE = .GlobalEnv$startup_probe
                        ),
                        value = 0L
                      )$value,
                      1L
                    ))
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                invisible(reticulate::py_config())
                reticulate::py_run_string("startup_value = 41")
                stopifnot(startup_calls == 1L)
                reticulate::py_to_r(reticulate::py$startup_value) + 1L
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(python="startup_value + 1")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_r_first_runs_selection_callback_once(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code("""
            Sys.unsetenv("RETICULATE_PYTHON")
            expected_python <- normalizePath(Sys.which("python3"))
            callback_calls <- 0L
            options(reticulate.python.beforeInitialized = function() {
              callback_calls <<- callback_calls + 1L
              reticulate::use_python(expected_python, required = TRUE)
            })
            config <- reticulate::py_config()
            stopifnot(callback_calls == 1L)
            stopifnot(identical(normalizePath(config$python), expected_python))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_retries_attachment_without_reinitializing_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with McpClient(binary, serve) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                invisible(suppressMessages(base::trace(
                  "py_run_string_impl",
                  tracer = quote({{
                    if (grepl("sys.executable  =", code, fixed = TRUE)) {{
                      startup_environment <<- Sys.getenv(c("VIRTUAL_ENV", "PATH", "R_SESSION_INITIALIZED"))
                      stop(structure(
                        list(message = "synthetic reticulate attach failure", call = NULL),
                        class = c(attachment_failure, "condition")
                      ))
                    }}
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                for (attachment_failure in c("error", "interrupt")) {{
                  failure <- tryCatch(
                    reticulate::py_config(),
                    error = conditionMessage,
                    interrupt = conditionMessage
                  )
                  stopifnot(!reticulate::py_available(initialize = FALSE))
                  stopifnot(grepl("synthetic reticulate attach failure", failure, fixed = TRUE))
                  stopifnot(identical(
                    Sys.getenv(names(startup_environment)), startup_environment
                  ))
                  stopifnot(identical(
                    .C(
                      getNativeSymbolInfo(
                        "mcp_console_probe_python_initialized",
                        PACKAGE = startup_probe
                      ),
                      value = 0L
                    )$value,
                    1L
                  ))
                }}
                invisible(suppressMessages(base::untrace(
                  "py_run_string_impl", where = asNamespace("reticulate")
                )))
                config <- reticulate::py_config()
                sys <- reticulate::import("sys", convert = FALSE)
                stopifnot(identical(config$executable, reticulate::py_to_r(sys$executable)))
                reticulate::py_run_string("startup_value = 41")
                reticulate::py_to_r(reticulate::py$startup_value) + 1L
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(python="startup_value + 1")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_selection_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            r_marker <- 41L
            invisible(suppressMessages(base::trace(
              "py_discover_config",
              tracer = quote({
                if (
                  !exists(
                    "selection_interrupted",
                    envir = .GlobalEnv,
                    inherits = FALSE
                  )
                ) {
                  assign("selection_interrupted", TRUE, envir = .GlobalEnv)
                  stop(base::structure(
                    base::list(message = "synthetic selection interrupt", call = NULL),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted selection ran the cell")
                """)
        )
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        client.send(python="r.r_marker + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restores_selection_environment_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            Sys.setenv(PYTHONPATH = "selection-original")
            original_environment <- Sys.getenv(
              c(
                "VIRTUAL_ENV",
                "R_SESSION_INITIALIZED",
                "PYTHONIOENCODING",
                "PATH",
                "LD_LIBRARY_PATH",
                "PYTHONPATH"
              ),
              unset = NA_character_
            )
            selection_env_interrupted <- FALSE
            invisible(suppressMessages(base::trace(
              "Sys.setenv",
              exit = quote({
                if ("PYTHONPATH" %in% names(list(...)) && !selection_env_interrupted) {
                  selection_env_interrupted <<- TRUE
                  stop(base::structure(
                    base::list(
                      message = "synthetic selection environment interrupt",
                      call = NULL
                    ),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = baseenv()
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted selection ran the cell")
                """)
        )
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        # fmt: r
        r = code("""
            stopifnot(selection_env_interrupted)
            stopifnot(identical(
              Sys.getenv(names(original_environment), unset = NA_character_),
              original_environment
            ))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        client.send(
            # fmt: r
            r=code("""
                stopifnot(identical(
                  Sys.getenv("PYTHONPATH"),
                  original_environment[["PYTHONPATH"]]
                ))
                42L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restores_virtualenv_after_selection_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code("""
            interrupted <- TRUE
            invisible(suppressMessages(base::trace(
              "Sys.setenv",
              exit = quote({
                if ("VIRTUAL_ENV" %in% names(list(...)) && !interrupted) {
                  interrupted <<- TRUE
                  stop(structure(
                    list(message = "selection interrupted", call = NULL),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = baseenv()
            )))
            for (previous in c(NA_character_, "before-selection")) {
              if (is.na(previous)) {
                Sys.unsetenv("VIRTUAL_ENV")
              } else {
                Sys.setenv(VIRTUAL_ENV = previous)
              }
              interrupted <- FALSE
              failure <- tryCatch(reticulate::py_config(), interrupt = conditionMessage)
              stopifnot(
                identical(failure, "selection interrupted"),
                identical(Sys.getenv("VIRTUAL_ENV", unset = NA_character_), previous),
                !reticulate::py_available(initialize = FALSE)
              )
            }
            invisible(suppressMessages(base::untrace("Sys.setenv", where = baseenv())))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_recovers_from_conflicting_requirements_before_python_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="startup_marker <- 41L")
        assert last_result_text(client) == "[done]", client.transcript[-1]
        result = client.send(
            python="raise AssertionError('failed preparation ran the cell')",
            requirements={"python": ["numpy<1", "numpy>=2"]},
        )
        assert result["isError"] is True, result
        output = last_result_text(client)
        assert "No solution found" in output, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_resolution_error(output)
        )
        client.send(
            # fmt: r
            r=code("""
                stopifnot(!reticulate::py_available(initialize = FALSE))
                startup_marker + 1L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="r.startup_marker + 1", requirements={"python": ["numpy"]})
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_serializes_selected_python_once_inside_interrupt_boundary(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            original_environment <- Sys.getenv(
              c(
                "VIRTUAL_ENV",
                "R_SESSION_INITIALIZED",
                "PYTHONIOENCODING",
                "PATH",
                "LD_LIBRARY_PATH",
                "PYTHONPATH"
              ),
              unset = NA_character_
            )
            selection_serializations <- 0L
            invisible(suppressMessages(base::trace(
              "toJSON",
              tracer = quote({
                if (
                  is.list(x) &&
                    identical(
                      names(x),
                      c("python", "libpython", "python_home")
                    )
                ) {
                  selection_serializations <<- selection_serializations + 1L
                  if (selection_serializations == 1L) {
                    base::stop(base::structure(
                      base::list(
                        message = "synthetic serialization interrupt",
                        call = NULL
                      ),
                      class = c("interrupt", "condition")
                    ))
                  }
                }
              }),
              print = FALSE,
              where = asNamespace("jsonlite")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(python="raise AssertionError('interrupted selection ran the cell')")
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        # fmt: r
        r = code("""
            stopifnot(!reticulate::py_available(initialize = FALSE))
            stopifnot(identical(
              Sys.getenv(names(original_environment), unset = NA_character_),
              original_environment
            ))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import importlib.util

            assert importlib.util.find_spec("yaml12") is not None
            42
            """)
        client.send(python=python, requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "42\n", client.transcript[-1]
        client.send(r="stopifnot(selection_serializations == 2L); 42L")
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        return client.finish()


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

            import _mcp_console

            def configure_again(*args):
                raise AssertionError("Python runtime configured twice")

            _mcp_console.configure_import_resolution = configure_again
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
