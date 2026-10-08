"""Runtime selection and implicit defaults through public MCP sessions."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_configuration import (
    configure,
    without_r,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import POSIX, R, SANDBOX, requires
from support.resolvers import bare_runtime_environment, expose_uv
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_locked_r_defaults_require_available_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    for policy in ("explicit", "startup_only"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            environment, _ = r_test_environment()
            environment = bare_runtime_environment(environment, root / "r-library")
            configure(root, {"r": {"resolution": policy}, "python": sys.executable})
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                result = client.send(r='cat("must not run\\n")')
                assert (
                    result.get("isError")
                    and "R preparation is unavailable" in result["content"][0]["text"]
                ), result
                client.finish_with_standard_error(expected_exit_status=1)
    return [{"r_defaults": "explicit and startup_only require startup preparation"}]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_bare_r_reset_keeps_empty_startup_declaration(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        environment = bare_runtime_environment(environment, root / "r-library")
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            inspected = client.send(requirements={"action": "get"})["structuredContent"]
            empty = {
                "r": [],
                "python": [],
                "duckdb": [],
                "python_version": [],
                "exclude_newer": None,
            }
            assert (
                inspected["requirements"] == inspected["startup_requirements"] == empty
            ), inspected
            reset = client.send(requirements={"action": "reset"})
            assert not reset.get("isError"), reset
            client.expect("bare reset retained\n", r='cat("bare reset retained\\n")')
            client.finish()
        (session,) = (root / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        declarations = [
            event["startup_requirements"]
            for event in events
            if event.get("startup_requirements") is not None
        ]
        assert declarations and all(
            declaration == empty for declaration in declarations
        ), declarations
        assert "  python-packages: []\n" in (session / "transcript.qmd").read_text()
    return [
        {
            "bare_r_baseline": "empty startup declaration, unchanged reset, and no recorded Python defaults"
        }
    ]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_bare_r_preserves_implicit_python_absence(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        environment = bare_runtime_environment(environment, root / "r-library")
        environment.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("bare R ready\n", r='cat("bare R ready\\n")')
            client.send(control="restart")
            client.expect("bare R retained\n", r='cat("bare R retained\\n")')
            client.finish()
        configure(root, {"python": {"managed": {"packages": []}}})
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            result = client.send(r='cat("must not run\\n")')
            assert (
                result.get("isError")
                and result["content"][0]["text"]
                == "dynamic environment resolution is unavailable; install `ir` or `uv` and restart MCP Console"
            ), result
            client.finish_with_standard_error(expected_exit_status=1)
    return [{"bare_r": "implicit Python absent; explicit managed Python requires uv"}]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_bare_worker_discards_inherited_managed_r_library(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = sys.executable
        environment["MCP_CONSOLE_R_LIBRARY"] = str(root / "retired-library")
        configure(root, {"r": {"resolution": "disabled"}})
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            source = 'stopifnot(Sys.getenv("MCP_CONSOLE_R_LIBRARY") == ""); cat("bare library ready\\n")'
            client.expect("bare library ready\n", r=source)
            client.send(control="restart")
            client.expect("bare library ready\n", r=source)
            client.finish()
    return [{"bare_worker": "inherited managed R library removed across generations"}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_conditional_duckdb_does_not_enable_inapplicable_defaults(
    binary: Path, execution: Execution
) -> Transcript:
    for requirement in (
        "duckdb; python_version < '3'",
        "duckdb; sys_platform == 'win32'",
    ):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            tools = root / "tools"
            tools.mkdir()
            expose_uv(tools)
            configure(
                root,
                {
                    "python": {
                        "managed": {"packages": [requirement], "resolution": "explicit"}
                    }
                },
            )
            with McpClient(
                binary, execution.serve("-c", "cache=host"), without_r(tools), root
            ) as client:
                client.initialize_and_list_tools()
                for restart in (False, True):
                    if restart:
                        client.send(control="restart", requirements={"action": "reset"})
                    client.expect(
                        "conditional environment ready\n",
                        python="import importlib.util; assert importlib.util.find_spec('duckdb') is None; print('conditional environment ready')",
                    )
                    inspected = client.send(requirements={"action": "get"})[
                        "structuredContent"
                    ]
                    assert inspected["requirements"]["duckdb"] == [], inspected
                    assert inspected["startup_requirements"]["duckdb"] == [], inspected
                    assert inspected["requirements"]["python"] == [requirement], (
                        inspected
                    )
                client.finish()
    return [
        {
            "conditional_duckdb": "false version and platform markers leave extension defaults empty"
        }
    ]


@requires(POSIX, SANDBOX)
def test_managed_fallback_uses_console_python_user_base(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        tools = root / "tools"
        tools.mkdir()
        expose_uv(tools)
        environment = without_r(tools)
        environment.update(
            XDG_CACHE_HOME=str(root / "cache"), PYTHONUSERBASE=str(root / "host-user")
        )
        configure(
            root,
            {
                "python": {
                    "first_available": [
                        {"existing": "absent"},
                        "active_venv",
                        {"managed": {"packages": []}},
                    ]
                }
            },
        )
        environment.pop("VIRTUAL_ENV", None)
        with McpClient(binary, SANDBOXED.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            # fmt: python
            source = code("""
                import os, site, sys
                from pathlib import Path

                expected = Path.cwd() / "cache/mcp-console/dependencies/python/user"
                assert Path(os.environ["PYTHONUSERBASE"]) == expected
                assert Path(site.getuserbase()) == expected
                assert not any(Path(path).is_relative_to(Path.cwd() / "host-user") for path in sys.path)
                print("managed user base isolated")
                """)
            client.expect("managed user base isolated\n", python=source)
            client.send(control="restart")
            client.expect("managed user base isolated\n", python=source)
            client.finish()
    return [{"managed_fallback": "Console user base retained across generations"}]


if __name__ == "__main__":
    run_this_suite(__file__)
