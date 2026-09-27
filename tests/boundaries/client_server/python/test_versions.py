#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.resolvers import (
    ir_cache_directory,
    resolver_fixture_directory,
    resolver_fixture_arguments,
    resolve_managed_python,
    named_requirement_error,
    normalize_duckdb_resolution_error,
    python_inventory_client,
    python_version_constraint_error,
    read_uv_resolver_records,
    recorded_python_preferences,
    recorded_tool_run_pythons,
    resolve_public_python_version,
    uv_python_row,
    write_python_executable,
    write_uv_python_inventories,
)
from support.suites import run_this_suite
from boundaries.client_server._harness import interrupt_paused_preparation


@executions(DIRECT, SANDBOXED)
def test_uses_current_r_library_for_managed_python_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    # The worker uses the current R library, while host uv must not inherit it.
    with resolver_fixture_directory(binary, execution) as temporary:
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        uv_record = temporary / "uv-environment.jsonl"
        r_libs_record = temporary / "uv-r-libs.jsonl"
        environment, _ = r_test_environment()
        environment.pop("R_LIBS", None)
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_R_LIBS_RECORD"] = str(r_libs_record)
        client = McpClient(
            binary,
            execution.serve(*resolver_fixture_arguments(environment)),
            environment,
        )
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            initial_r_library <- .libPaths()[[managed_index]]
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"

        def current_r_library() -> str:
            # fmt: r
            r = code(r"""
                managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
                cat(jsonlite::toJSON(.libPaths()[[managed_index]], auto_unbox = TRUE))
                """)
            client.send(r=r)
            output = last_result_text(client)
            library = json.loads(output)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                '"<current managed R library>"'
            )
            return library

        def assert_resolver_ignored_r_library() -> None:
            records = [
                json.loads(line)
                for line in r_libs_record.read_text(encoding="utf-8").splitlines()
            ]
            assert records, "managed Python resolution did not invoke uv"
            expected = (
                str(temporary.parent / "r/bootstrap")
                if execution.name == "sandbox"
                else None
            )
            assert all(record == expected for record in records), records

        client.send(requirements={"r": ["zeallot"]})
        assert last_result_text(client) == "[prepared]"
        assert Path(current_r_library()).is_dir()
        uv_record.write_text("", encoding="utf-8")
        r_libs_record.write_text("", encoding="utf-8")
        # Printing unconstrained requirements asks the host for the default
        # Python version without creating a new environment.
        # fmt: r
        r = code(r"""
            invisible(capture.output(print(reticulate::py_require())))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        assert_resolver_ignored_r_library()

        uv_record.write_text("", encoding="utf-8")
        r_libs_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        assert_resolver_ignored_r_library()

        uv_record.write_text("", encoding="utf-8")
        r_libs_record.write_text("", encoding="utf-8")
        client.send(
            control="restart",
            requirements={"r": ["praise"], "python": ["six"]},
        )
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        assert Path(current_r_library()).is_dir()
        assert_resolver_ignored_r_library()
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_validates_registry_only_python_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        uv_record = temporary / "uv-environment.jsonl"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        environment = os.environ.copy()
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        client = McpClient(
            binary,
            execution.serve(*resolver_fixture_arguments(environment)),
            environment,
        )
        client.initialize_and_list_tools()
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
            call_shape = {} if index % 2 == 0 else {"control": "restart"}
            client.send(
                **call_shape,
                requirements={
                    "r": ["praise"],
                    "python": [requirement],
                    "duckdb": [duckdb_extension],
                },
            )
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert last_result_text(client) == named_requirement_error(requirement)
        assert uv_record.read_text(encoding="utf-8") == ""

        client.send(
            requirements={"duckdb": [duckdb_extension]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        result["content"][0]["text"] = normalize_duckdb_resolution_error(
            result["content"][0]["text"], duckdb_extension
        )

        prepared = "requests[socks]>=2,<3; python_version < '0'"
        client.send(
            requirements={"python": [prepared]},
        )
        assert last_result_text(client) == "[prepared]"
        assert uv_record.read_text(encoding="utf-8") != ""
        restarted = "urllib3!=2.0.0; python_version < '0'"
        client.send(
            control="restart",
            requirements={"python": [restarted]},
        )
        assert last_result_text(client) == "[starting new worker]\n[idle]"

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
        assert last_result_text(client) == "[done]", client.transcript[-1]

        worker_executable = temporary / "worker-python"
        worker_retained_selector = temporary / "worker-retained-python"
        worker_installation = temporary / "worker-python-installation"
        for selector in (worker_executable, worker_retained_selector):
            selector.write_text(
                """#!/bin/sh
touch "$0.executed"
exit 97
""",
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
        output = last_result_text(client)
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
        assert last_result_text(client) == "[1] TRUE\n"

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
        transcript = client.finish()
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


@executions(DIRECT, SANDBOXED)
def test_recovers_from_python_version_resolution_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        uv = Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        failure_marker = temporary / "fail-version-resolution"
        environment = os.environ.copy()
        environment["RETICULATE_UV"] = str(uv)
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(temporary / "uv.jsonl")
        environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"] = str(failure_marker)
        environment["MCP_CONSOLE_TEST_UV_FAILURE_ARGUMENT"] = "list"

        client = McpClient(
            binary,
            execution.serve(*resolver_fixture_arguments(environment)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="worker_pid <- Sys.getpid()")
        assert last_result_text(client) == "[done]", client.transcript[-1]
        failure_marker.touch()
        # fmt: r
        r = code(r"""
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
        assert last_result_text(client) == "[1] TRUE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_resolves_python_version_inventory_semantics(
    binary: Path, execution: Execution
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        client, inventories, arguments = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=Path(sys.executable),
        )
        write_uv_python_inventories(
            inventories,
            {
                "only-managed": [
                    uv_python_row("3.15.0a5"),
                    uv_python_row("3.14.3"),
                    uv_python_row("3.13.11"),
                    uv_python_row("3.12.11"),
                    uv_python_row("3.12.12"),
                    uv_python_row("3.11.14"),
                ]
            },
        )

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        assert recorded_python_preferences(arguments) == ["only-managed"]
        assert recorded_tool_run_pythons(arguments) == ["3.12.12"]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_resolves_python_version_constraint_semantics(
    binary: Path, execution: Execution
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        client, inventories, _ = python_inventory_client(binary, execution, temporary)

        write_uv_python_inventories(
            inventories,
            {
                "only-managed": [
                    uv_python_row("3.12.12"),
                    uv_python_row("3.11.14"),
                ]
            },
        )
        normalized = resolve_public_python_version(client, ["v3.12.12"])
        assert normalized == "3.12.12\n", normalized

        write_uv_python_inventories(
            inventories,
            {"only-managed": [uv_python_row("3.15.0a5")]},
        )
        prerelease = resolve_public_python_version(client, ["==3.15.0a5"])
        assert prerelease == "3.15.0a5\n", prerelease

        write_uv_python_inventories(
            inventories,
            {
                "only-managed": [
                    uv_python_row("3.12.0a5"),
                    uv_python_row("3.11.14"),
                ]
            },
        )
        numeric_equal = resolve_public_python_version(client, ["==3.12.0"])
        assert 'constraints: "==3.12.0"' in numeric_equal, numeric_equal

        numeric_not_equal = resolve_public_python_version(
            client,
            [">=3.12.0a1", "!=3.12.0"],
        )
        assert numeric_not_equal == "3.12.0a5\n", numeric_not_equal
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_falls_back_after_filtering_unsupported_python_versions(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        client, inventories, arguments = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=Path(sys.executable),
        )
        write_uv_python_inventories(
            inventories,
            {
                "only-managed": [
                    uv_python_row(
                        "3.13.14",
                        variant="freethreaded",
                    ),
                    uv_python_row(
                        "3.11.15",
                        implementation="pypy",
                    ),
                ],
                "only-system": [
                    uv_python_row(
                        "3.11.14",
                        path="/usr/bin/python3.11",
                        url=None,
                    )
                ],
            },
        )

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        assert recorded_python_preferences(arguments) == [
            "only-managed",
            "only-system",
        ]
        assert recorded_tool_run_pythons(arguments) == ["3.11.14"]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_respects_system_python_preference_with_custom_install_directory(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        install_directory = temporary / "managed-python"
        install_directory.mkdir()
        client, inventories, arguments = python_inventory_client(
            binary,
            execution,
            temporary,
            preference="system",
            install_directory=install_directory,
            resolver_python=Path(sys.executable),
        )
        write_uv_python_inventories(
            inventories,
            {
                "only-managed": [
                    uv_python_row("3.14.3"),
                    uv_python_row(
                        "3.12.12",
                        path=install_directory / "cpython-3.12/bin/python3.12",
                        url=None,
                    ),
                ],
                "only-system": [
                    uv_python_row(
                        "3.13.11",
                        path="/usr/local/bin/python3.13",
                        url=None,
                    ),
                    uv_python_row(
                        "3.9.6",
                        path="/usr/bin/python3",
                        url=None,
                    ),
                ],
            },
        )

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        assert recorded_python_preferences(arguments) == [
            "only-managed",
            "only-system",
        ]
        assert recorded_tool_run_pythons(arguments) == ["3.13.11"]
        return client.finish()


@executions(DIRECT)
def test_uses_reticulate_managed_uv_for_python_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        original_path = os.environ.get("PATH", "")
        real_uv = shutil.which("uv", path=original_path)
        assert real_uv is not None, "real uv is required"
        host_ir_cache = ir_cache_directory(os.environ.copy())
        r_home = subprocess.run(
            ["R", "RHOME"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        real_rscript = Path(r_home) / "bin/Rscript"
        r_user_cache = temporary / "r-user-cache"
        r_environment = os.environ.copy()
        r_environment["R_USER_CACHE_DIR"] = str(r_user_cache)
        managed_uv = Path(
            subprocess.run(
                [
                    real_rscript,
                    "--vanilla",
                    "-e",
                    "cat(file.path(tools::R_user_dir('reticulate', 'cache'), 'uv', 'bin', 'uv'))",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=r_environment,
            ).stdout.strip()
        )
        managed_root = managed_uv.parent.parent
        managed_uv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment",
            managed_uv,
        )
        managed_uv.chmod(0o755)

        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        path_uv = fake_bin / "uv"
        path_uv_log = temporary / "path-uv.log"
        write_python_executable(
            path_uv,
            # fmt: python
            code(r"""
                #!/usr/bin/env python3
                import os
                from pathlib import Path

                Path(os.environ["MCP_CONSOLE_TEST_PATH_UV_LOG"]).write_text(
                    "called\n",
                    encoding="utf-8",
                )
                raise SystemExit(97)
                """),
        )

        uv_record = temporary / "uv.jsonl"
        resolver_record = temporary / "uv-resolver.jsonl"
        inventories = temporary / "uv-python-inventories.json"
        intercept_marker = temporary / "intercept-managed-uv"
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        environment["RETICULATE_UV"] = "managed"
        environment["R_USER_CACHE_DIR"] = str(r_user_cache)
        environment["IR_CACHE_DIR"] = host_ir_cache
        environment["UV_CACHE_DIR"] = str(temporary / "wrong-cache")
        environment["UV_PYTHON_INSTALL_DIR"] = str(temporary / "wrong-python")
        environment["PATH"] = os.pathsep.join((str(fake_bin), original_path))
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_UV_RESOLVER_RECORD"] = str(resolver_record)
        environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(inventories)
        environment["MCP_CONSOLE_TEST_UV_INTERCEPT_MARKER"] = str(intercept_marker)
        environment["MCP_CONSOLE_TEST_UV_PYTHON"] = sys.executable
        environment["MCP_CONSOLE_TEST_PATH_UV_LOG"] = str(path_uv_log)

        client = McpClient(
            binary,
            execution.serve(*resolver_fixture_arguments(environment)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(requirements={"r": ["DBI"]})
        assert last_result_text(client) == "[prepared]"
        uv_record.write_text("", encoding="utf-8")
        resolver_record.write_text("", encoding="utf-8")
        write_uv_python_inventories(
            inventories,
            {"only-managed": [uv_python_row("3.12.9")]},
        )
        intercept_marker.touch()

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        assert not path_uv_log.exists(), "PATH uv handled managed resolution"
        records = read_uv_resolver_records(resolver_record)
        version_lists = [
            record
            for record in records
            if record["arguments"][:2] == ["python", "list"]
        ]
        tool_runs = [
            record for record in records if record["arguments"][:2] == ["tool", "run"]
        ]
        assert len(version_lists) == 1, records
        assert len(tool_runs) == 1, records
        for record in (*version_lists, *tool_runs):
            assert record["RETICULATE_UV"] == "managed", record
            assert (
                Path(str(record["UV_CACHE_DIR"])).resolve()
                == (managed_root / "cache").resolve()
            ), record
            assert (
                Path(str(record["UV_PYTHON_INSTALL_DIR"])).resolve()
                == (managed_root / "python").resolve()
            ), record
        tool_arguments = tool_runs[0]["arguments"]
        assert tool_arguments[tool_arguments.index("--python") + 1] == "3.12.9"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retains_managed_python_when_uv_caching_is_disabled(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        resolver_record = temporary / "uv-resolver.jsonl"
        client, _, _ = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=Path(sys.executable),
            resolver_record=resolver_record,
            extra_environment={"UV_NO_CACHE": "1"},
        )

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        records = read_uv_resolver_records(resolver_record)
        version_lists = [
            record
            for record in records
            if record["arguments"][:2] == ["python", "list"]
        ]
        tool_runs = [
            record for record in records if record["arguments"][:2] == ["tool", "run"]
        ]
        assert version_lists, records
        assert tool_runs, records
        assert all(
            record["UV_NO_CACHE"] == ("0" if execution == SANDBOXED else "1")
            for record in version_lists
        ), version_lists
        assert all(record["UV_NO_CACHE"] is None for record in tool_runs), tool_runs
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_removes_disabled_uv_python_source_aliases(
    binary: Path, execution: Execution
) -> Transcript:
    with resolver_fixture_directory(binary, execution) as temporary:
        resolver_record = temporary / "uv-resolver.jsonl"
        client, _, _ = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=Path(sys.executable),
            resolver_record=resolver_record,
            extra_environment={
                "UV_MANAGED_PYTHON": "false",
                "UV_NO_MANAGED_PYTHON": "n",
            },
        )

        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        records = read_uv_resolver_records(resolver_record)
        assert records, "managed Python resolution did not invoke uv"
        assert all(record["UV_MANAGED_PYTHON"] is None for record in records), records
        assert all(record["UV_NO_MANAGED_PYTHON"] is None for record in records), (
            records
        )
        return client.finish()


def probe_python(
    binary: Path, execution: Execution, directory: Path, source: str
) -> Path:
    with tempfile.TemporaryDirectory() as workspace:
        python = resolve_managed_python(binary, execution, Path(workspace))
    venv = directory / "probe-python"
    subprocess.run(
        [python, "-m", "venv", "--without-pip", venv], check=True, capture_output=True
    )
    site = next((venv / "lib").glob("python*/site-packages"))
    (site / "sitecustomize.py").write_text(source)
    return venv / "bin/python"


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_cache_warmup_without_committing(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with (
        resolver_fixture_directory(binary, execution) as temporary,
        ExitStack() as cleanup,
    ):
        preflight_warmup = temporary / "preflight-warmup"
        blocked_warmup = temporary / "blocked-warmup"
        warmup_started = FifoCheckpoint.create(temporary / "warmup-started")
        cleanup.callback(warmup_started.close)
        warmup_release = FifoCheckpoint.create(temporary / "warmup-release")
        cleanup.callback(warmup_release.close)
        python = probe_python(
            binary,
            execution,
            temporary,
            # fmt: python
            code("""
                import os, sys
                from pathlib import Path

                if "import matplotlib.font_manager" in sys.orig_argv:
                    preflight = Path(os.environ["MCP_CONSOLE_TEST_PREFLIGHT_WARMUP"])
                    blocked = Path(os.environ["MCP_CONSOLE_TEST_BLOCKED_WARMUP"])
                    if not preflight.exists():
                        preflight.touch()
                    elif not blocked.exists():
                        blocked.touch()
                        with open(
                            os.environ["MCP_CONSOLE_TEST_WARMUP_STARTED"], "wb", buffering=0
                        ) as started:
                            started.write(b"1")
                        with open(
                            os.environ["MCP_CONSOLE_TEST_WARMUP_RELEASE"], "rb", buffering=0
                        ) as release:
                            assert release.read(1) == b"1"
                """),
        )
        client, _, arguments = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=python,
            extra_environment={
                "MCP_CONSOLE_TEST_PREFLIGHT_WARMUP": str(preflight_warmup),
                "MCP_CONSOLE_TEST_BLOCKED_WARMUP": str(blocked_warmup),
                "MCP_CONSOLE_TEST_WARMUP_STARTED": str(warmup_started.path),
                "MCP_CONSOLE_TEST_WARMUP_RELEASE": str(warmup_release.path),
            },
        )
        with client:
            preparation = client.start_send(requirements={"python": ["py-yaml12"]})
            warmup_started.wait("Python cache warmup started")
            interrupt_paused_preparation(client, warmup_release)
            client.receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {"type": "text", "text": "dependency resolution interrupted"}
                ],
                "isError": True,
            }, preparation
            client.send()
            assert last_result_text(client) == "\n[idle]"
            client.send(requirements={"python": ["py-yaml12"]})
            assert last_result_text(client) == "[prepared]"
            assert len(recorded_tool_run_pythons(arguments)) == 2
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_stops_before_cache_warmup_after_python_resolver_interrupt(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with (
        resolver_fixture_directory(binary, execution) as temporary,
        ExitStack() as cleanup,
    ):
        block_tool_run = temporary / "block-tool-run"
        tool_run_started = FifoCheckpoint.create(temporary / "tool-run-started")
        cleanup.callback(tool_run_started.close)
        tool_run_release = FifoCheckpoint.create(temporary / "tool-run-release")
        cleanup.callback(tool_run_release.close)
        unexpected_warmup = temporary / "unexpected-warmup"
        python = probe_python(
            binary,
            execution,
            temporary,
            # fmt: python
            code("""
                import os, sys
                from pathlib import Path

                if Path(os.environ["MCP_CONSOLE_TEST_BLOCK_TOOL_RUN"]).exists():
                    if "import matplotlib.font_manager" in sys.orig_argv:
                        Path(os.environ["MCP_CONSOLE_TEST_UNEXPECTED_WARMUP"]).touch()
                    elif "-I" not in sys.orig_argv:
                        with open(
                            os.environ["MCP_CONSOLE_TEST_TOOL_RUN_STARTED"], "wb", buffering=0
                        ) as started:
                            started.write(b"1")
                        with open(
                            os.environ["MCP_CONSOLE_TEST_TOOL_RUN_RELEASE"], "rb", buffering=0
                        ) as release:
                            assert release.read(1) == b"1"
                """),
        )
        client, _, arguments = python_inventory_client(
            binary,
            execution,
            temporary,
            resolver_python=python,
            extra_environment={
                "MCP_CONSOLE_TEST_BLOCK_TOOL_RUN": str(block_tool_run),
                "MCP_CONSOLE_TEST_TOOL_RUN_STARTED": str(tool_run_started.path),
                "MCP_CONSOLE_TEST_TOOL_RUN_RELEASE": str(tool_run_release.path),
                "MCP_CONSOLE_TEST_UNEXPECTED_WARMUP": str(unexpected_warmup),
            },
        )
        with client:
            block_tool_run.touch()
            preparation = client.start_send(requirements={"python": ["py-yaml12"]})
            tool_run_started.wait("Python resolver started")
            interrupt_paused_preparation(client, tool_run_release)
            client.receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {"type": "text", "text": "dependency resolution interrupted"}
                ],
                "isError": True,
            }, preparation
            assert not unexpected_warmup.exists(), (
                "cache warmup started after interruption"
            )
            client.send()
            assert last_result_text(client) == "\n[idle]"
            block_tool_run.unlink()
            client.send(requirements={"python": ["py-yaml12"]})
            assert last_result_text(client) == "[prepared]"
            assert len(recorded_tool_run_pythons(arguments)) == 2
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
