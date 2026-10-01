#!/usr/bin/env -S uv run --script

import os
import subprocess
import shutil
import sys
import tempfile
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_startup import (
    isolated_python,
    selected_python,
)
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)
from support.assertions import last_result_text, wait_for_evaluation_output
from support.allocations import AllocationProfile
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.requirements import R, NATIVE_FIXTURES, requires
from support.r import r_test_environment
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@contextmanager
def python_bootstrap(
    binary: Path,
    execution: Execution,
    *,
    sans_r: bool,
    server_environment: dict[str, str] | None = None,
):
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import builtins
                import sys
                from pathlib import Path

                if sys.argv[0] != "-c":
                    builtins.bootstrap_runs = 1
                    print("Python startup")
                    with Path({str(reached.path)!r}).open("wb", buffering=0) as stream:
                        assert stream.write(b"1") == 1
                    with Path({str(release.path)!r}).open("rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                    builtins.bootstrap_input = input("startup> ")
                """),
        )
        environment = selected_python(root, python)
        environment["MCP_CONSOLE_LANGUAGES"] = "python,sql"
        environment.update(server_environment or {})
        if sans_r:
            environment.pop("R_HOME", None)
            environment["PATH"] = str(root)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            environment,
            root,
        ) as client:
            try:
                reached.wait("embedded Python starts before initialize or send")
                yield client, release
            finally:
                release.release()


def queued_input(client: McpClient, release: FifoCheckpoint) -> list:
    client.initialize_and_list_tools()
    client.request("ping")
    wait_for_evaluation_output(
        client, "Python startup\n\n[idle]", "startup output before first cell"
    )
    client.send(
        python="import builtins; counter = 1; builtins.bootstrap_input", timeout_ms=0
    )
    assert last_result_text(client) == RUNNING, repr(last_result_text(client))
    client.send(timeout_ms=20)
    assert last_result_text(client) == RUNNING, repr(last_result_text(client))
    client.send(python="counter += 1", timeout_ms=0)
    assert client.transcript[-1]["result"]["isError"]
    release.release()
    wait_for_evaluation_output(
        client, '[input requested: "startup> "]\n[waiting for stdin]', "startup input"
    )
    client.send(stdin="retained\n")
    assert last_result_text(client) == "'retained'\n", last_result_text(client)
    client.send(python="counter, builtins.bootstrap_runs")
    assert last_result_text(client) == "(1, 1)\n", last_result_text(client)
    client.send(python="raise RuntimeError('first user filenames')")
    assert "<mcp-console:python:e3>" in last_result_text(client), last_result_text(
        client
    )
    return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_sans_r_starts_before_send_and_preserves_queued_input(
    binary: Path, execution: Execution
) -> list:
    with python_bootstrap(binary, execution, sans_r=True) as (client, release):
        return queued_input(client, release)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_selected_python_starts_independently_of_r(
    binary: Path, execution: Execution
) -> list:
    with python_bootstrap(binary, execution, sans_r=False) as (client, release):
        return queued_input(client, release)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_hooks_run_before_send(binary: Path, execution: Execution) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        library = root / "library"
        library.mkdir()
        environment, rscript = r_test_environment()
        subprocess.run(
            [
                rscript.with_name("R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                Path(__file__).resolve().parents[3] / "fixtures/bootstrap_r",
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
        environment.update(
            MCP_CONSOLE_LANGUAGES="r",
            R_LIBS=os.pathsep.join(
                filter(None, (str(library), environment.get("R_LIBS")))
            ),
            R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsolebootstrap",
            MCP_CONSOLE_TEST_BOOTSTRAP_REACHED=str(reached.path),
            MCP_CONSOLE_TEST_BOOTSTRAP_RELEASE=str(release.path),
        )
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            environment,
            root,
        ) as client:
            try:
                reached.wait("R startup package before initialize or send")
                client.initialize_and_list_tools()
                client.request("ping")
                client.send(r="bootstrap_value", timeout_ms=0)
                assert last_result_text(client).endswith(RUNNING)
                release.release()
                wait_for_evaluation_output(
                    client,
                    '[input requested: "R startup> "]\n[waiting for stdin]',
                    "R startup input",
                )
                client.send(stdin="R state\n")
                assert last_result_text(client) == '[1] "R state"\n', last_result_text(
                    client
                )
                client.send(
                    r='stopifnot(!"duckdb" %in% loadedNamespaces()); bootstrap_value'
                )
                assert last_result_text(client) == '[1] "R state"\n'
                return client.finish()[3:]
            finally:
                release.release()


@executions(DIRECT, SANDBOXED)
def test_closure_retires_blocked_bootstrap(binary: Path, execution: Execution) -> list:
    with python_bootstrap(binary, execution, sans_r=True) as (client, release):
        client.initialize_and_list_tools()
        client.close()
        return [
            {
                "blocked_bootstrap_retired": True,
                "server_status": client.process.returncode,
            }
        ]


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_cancelled_response_preserves_bootstrap_and_first_cell(
    binary: Path, execution: Execution
) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        profile = resources.enter_context(closing(AllocationProfile(root)))
        result_reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "result-reached"))
        )
        result_release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "result-release"))
        )
        client, release = resources.enter_context(
            python_bootstrap(
                binary,
                execution,
                sans_r=True,
                server_environment={
                    **profile.environment,
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(result_reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(result_release.path),
                },
            )
        )
        client.initialize_and_list_tools()
        wait_for_evaluation_output(client, "Python startup\n\n[idle]", "startup output")
        profile.pause_results(True)
        try:
            pending = client.start_send(python="counter = 1; counter", timeout_ms=0)
            result_reached.wait("first cell response owns delivery")
            client.notify("notifications/cancelled", requestId=pending["id"])
            client.request("ping")
        finally:
            profile.pause_results(False)
            result_release.release()
        client.send(timeout_ms=0)
        assert last_result_text(client) == RUNNING
        assert "result" not in pending
        release.release()
        wait_for_evaluation_output(
            client,
            '[input requested: "startup> "]\n[waiting for stdin]',
            "cancelled response startup input",
        )
        client.send(stdin="continue\n")
        assert last_result_text(client) == "1\n", last_result_text(client)
        client.send(python="counter")
        assert last_result_text(client) == "1\n"
        return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_first_declaration_replaces_blocked_bootstrap(
    binary: Path, execution: Execution
) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        commands = root / "commands"
        commands.mkdir()
        (commands / "uv").symlink_to(shutil.which("uv"))
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        identities = root / "workers"
        (root / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import os
                import sys
                from pathlib import Path

                if "_mcp_console_services" in sys.modules:
                    with Path({str(identities)!r}).open("a") as stream:
                        stream.write(str(os.getpid()) + "\\n")
                    with Path({str(reached.path)!r}).open("wb", buffering=0) as stream:
                        assert stream.write(b"1") == 1
                    with Path({str(release.path)!r}).open("rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                """),
        )
        environment = without_r_environment(commands)
        environment["PYTHONPATH"] = str(root)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            environment,
            root,
            response_timeout=600,
        ) as client:
            try:
                reached.wait("first generation bootstrap", timeout=600)
                client.initialize_and_list_tools()
                client.send(
                    python="counter = 1; input('first cell> ')",
                    requirements={"action": "set"},
                    stdin="retained input\n",
                    timeout_ms=0,
                )
                assert last_result_text(client) == RUNNING
                reached.wait("replacement generation bootstrap", timeout=600)
                workers = identities.read_text().splitlines()
                assert len(workers) == 2 and workers[0] != workers[1], workers
                release.release()
                wait_for_evaluation_output(
                    client,
                    "[input requested: \"first cell> \"]\n'retained input'\n",
                    "first cell after replacement",
                )
                client.send(python="counter")
                assert last_result_text(client) == "1\n"
                client.send(requirements={"action": "reset"})
                assert client.transcript[-1]["result"]["isError"]
                assert "explicit restart" in str(client.transcript[-1]["result"])
                return client.finish()[3:]
            finally:
                release.release()


if __name__ == "__main__":
    run_this_suite(__file__)
