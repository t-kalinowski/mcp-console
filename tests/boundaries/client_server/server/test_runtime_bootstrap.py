#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
import time
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
from support.requirements import R, NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.resolvers import (
    checkpoint_uv_environment,
    recording_uv_environment,
    write_uv_python_inventories,
    uv_python_row,
    FIXTURES,
)
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
    host_process_id,
)
from support.r import r_test_environment
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@contextmanager
def python_bootstrap(
    binary: Path,
    execution: Execution,
    *,
    sans_r: bool,
    output: bool = True,
    fail_once: bool = False,
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
        failure = root / "fail"
        if fail_once:
            failure.touch()
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import builtins
                import os
                import sys
                from pathlib import Path

                if sys.argv[0] != "-c":
                    builtins.bootstrap_runs = 1
                    Path({str(root / "worker")!r}).write_text(str(os.getpid()))
                    if {output!r}:
                        print("Python startup")
                    with Path({str(reached.path)!r}).open("wb", buffering=0) as stream:
                        assert stream.write(b"1") == 1
                    with Path({str(release.path)!r}).open("rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                    failure = Path({str(failure)!r})
                    if failure.exists():
                        failure.unlink()
                        os._exit(47)
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


def bootstrap_output(client: McpClient, expected: str) -> None:
    start = len(client.transcript)
    collected = ""
    deadline = time.monotonic() + 3
    while True:
        result = client.send(timeout_ms=0)
        assert not result["isError"], result
        output = last_result_text(client)
        assert output.endswith("\n[idle]"), repr(output)
        collected += output.removesuffix("\n[idle]")
        assert expected.startswith(collected), repr(collected)
        if collected == expected:
            break
        assert time.monotonic() < deadline, "bootstrap output did not reach server"
    result["content"][0]["text"] = collected + "\n[idle]"
    client.transcript[start:] = [client.transcript[-1]]


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_retires_bootstrap_before_new_cell(
    binary: Path, execution: Execution
) -> list:
    with python_bootstrap(binary, execution, sans_r=True, output=False) as (
        client,
        release,
    ):
        root = release.path.parent
        old = capture_process_identity(
            host_process_id(int((root / "worker").read_text()), client.process.pid)
        )
        client.initialize_and_list_tools()
        client.send(
            control="restart",
            python="import builtins; builtins.bootstrap_input",
            stdin="new generation\n",
            timeout_ms=0,
        )
        assert (
            last_result_text(client)
            == "[worker stopped: in-memory state lost]\n[starting new worker]\n"
            + RUNNING
        ), repr(last_result_text(client))
        with closing(FifoCheckpoint.attach(root / "reached")) as reached:
            reached.wait("replacement hook is running")
        assert not live_processes([old]), "restart left the old bootstrap live"
        assert int((root / "worker").read_text()) != old[0]
        release.release()
        wait_for_evaluation_output(
            client,
            "[input requested: \"startup> \"]\n'new generation'\n[done]",
            "replacement cell input",
        )
        return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_failed_bootstrap_withholds_cell_and_replaces_worker(
    binary: Path, execution: Execution
) -> list:
    with python_bootstrap(
        binary, execution, sans_r=True, output=False, fail_once=True
    ) as (client, release):
        root = release.path.parent
        old = capture_process_identity(
            host_process_id(int((root / "worker").read_text()), client.process.pid)
        )
        client.initialize_and_list_tools()
        client.send(python="never_run = True", timeout_ms=0)
        assert last_result_text(client) == RUNNING
        release.release()
        output = wait_for_evaluation_output(
            client,
            lambda text: (
                "[worker exited with status 47]" in text and text.endswith("[idle]")
            ),
            "failed bootstrap replacement",
            expected_error=True,
        )
        assert (
            "[worker stopped: in-memory state lost]\n[starting new worker]" in output
        ), output
        with closing(FifoCheckpoint.attach(root / "reached")) as reached:
            reached.wait("replacement hook after failure")
        assert not live_processes([old]), "failed bootstrap survived replacement"
        client.send(
            python="assert 'never_run' not in globals(); counter = 1; counter",
            stdin="replacement\n",
            timeout_ms=0,
        )
        assert last_result_text(client) == RUNNING
        release.release()
        wait_for_evaluation_output(
            client,
            '[input requested: "startup> "]\n1\n',
            "new cell after failed bootstrap",
        )
        client.send(python="counter")
        assert last_result_text(client) == "1\n"
        return client.finish()[3:]


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
def test_short_startup_transcript(binary: Path, execution: Execution) -> list:
    with python_bootstrap(binary, execution, sans_r=True) as (client, release):
        client.initialize_and_list_tools()
        wait_for_evaluation_output(client, "Python startup\n\n[idle]", "startup output")
        client.send(
            python="import builtins; builtins.bootstrap_input",
            stdin="hello\n",
            timeout_ms=0,
        )
        assert last_result_text(client) == RUNNING
        release.release()
        wait_for_evaluation_output(
            client,
            "[input requested: \"startup> \"]\n'hello'\n",
            "startup and first cell",
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
        python, site = isolated_python(root)
        python_started = root / "disabled-python-started"
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import sys
                from pathlib import Path

                if sys.argv[0] != "-c":
                    Path({str(python_started)!r}).write_text("Python initialized")
                """)
        )
        environment.update(RETICULATE_PYTHON=str(python), PYTHONPATH=str(site))
        if execution == SANDBOXED:
            # Preserve the fixture's workload inputs, but leave language selection
            # solely in the controller environment. Native inheritance removes it.
            workload = {
                name: environment[name]
                for name in (
                    "HOME",
                    "PATH",
                    "R_HOME",
                    "R_PROFILE_USER",
                    "R_LIBS",
                    "R_DEFAULT_PACKAGES",
                    "PYTHONPATH",
                    "MCP_CONSOLE_TEST_BOOTSTRAP_REACHED",
                    "MCP_CONSOLE_TEST_BOOTSTRAP_RELEASE",
                )
            }
            configuration = root / ".agents/console/config.yaml"
            configuration.parent.mkdir(parents=True)
            configuration.write_text(
                json.dumps(
                    {
                        "sandbox": {
                            "inherit_environment": False,
                            "environment": workload,
                        }
                    }
                )
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
                schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
                assert "r" in schema["properties"]
                assert "python" not in schema["properties"]
                assert "sql" not in schema["properties"]
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
                assert not python_started.exists(), "disabled Python initialized"
                return client.finish()[3:]
            finally:
                release.release()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_closure_retires_blocked_bootstrap(binary: Path, execution: Execution) -> list:
    with python_bootstrap(binary, execution, sans_r=True) as (client, release):
        client.initialize_and_list_tools()
        descendants = []
        pending = [capture_process_identity(client.process.pid)]
        while pending:
            children = child_process_identities(pending.pop())
            descendants.extend(children)
            pending.extend(children)
        try:
            client.close()
            assert not live_processes(descendants), (
                "bootstrap resources survived closure"
            )
            return [
                {
                    "blocked_bootstrap_retired": True,
                    "server_status": client.process.returncode,
                }
            ]
        finally:
            kill_processes(descendants)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_bootstrap_resolves_python_version_and_import(
    binary: Path, execution: Execution
) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        complete = resources.enter_context(
            closing(FifoCheckpoint.create(root / "complete"))
        )
        environment, resolving, resolved = checkpoint_uv_environment(root, "py-yaml12")
        resources.callback(resolving.close)
        resources.callback(resolved.close)
        r_environment, rscript = r_test_environment()
        environment.update(r_environment)
        environment["RETICULATE_PYTHON"] = ""
        library = root / "library"
        library.mkdir()
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
        script = root / "startup.R"
        script.write_text(
            # fmt: r
            code("""
                version <- reticulate:::resolve_python_version(">=3.13")
                stopifnot(grepl("^[0-9]+[.][0-9]+[.][0-9]+$", version))
                cat("Python version resolved\\n")
                module <- reticulate::import("yaml12", convert = FALSE)
                stopifnot(identical(reticulate::py_to_r(module$`__name__`), "yaml12"))
                cat("Python import resolved\\n")
                stopifnot(!"duckdb" %in% loadedNamespaces())
                stopifnot(!reticulate::py_eval("'duckdb' in __import__('sys').modules"))
                bootstrap_value <- 42L
                completed <- fifo(
                  Sys.getenv("MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETE"),
                  "wb",
                  blocking = TRUE
                )
                writeBin(charToRaw("1"), completed)
                close(completed)
                """),
        )
        environment.update(
            MCP_CONSOLE_LANGUAGES="r",
            R_LIBS=os.pathsep.join(
                filter(None, (str(library), environment.get("R_LIBS")))
            ),
            R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsolebootstrap",
            MCP_CONSOLE_TEST_BOOTSTRAP_REACHED=str(reached.path),
            MCP_CONSOLE_TEST_BOOTSTRAP_RELEASE=str(release.path),
            MCP_CONSOLE_TEST_BOOTSTRAP_SCRIPT=str(script),
            MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETE=str(complete.path),
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
                reached.wait("R hook before initialize or send")
                client.initialize_and_list_tools()
                release.release()
                resolving.wait("bootstrap automatic import reaches host resolver")
                client.request("ping")
                bootstrap_output(client, "Python version resolved\n")
                resolved.release()
                complete.wait("bootstrap import finished", timeout=600)
                bootstrap_output(
                    client,
                    "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\nPython import resolved\n",
                )
                manifest = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                assert "py-yaml12" in manifest["requirements"]["python"], manifest
                client.transcript[-1]["result"] = (
                    "<bootstrap import committed py-yaml12>"
                )
                client.send(r="bootstrap_value")
                assert last_result_text(client) == "[1] 42\n"
                return client.finish()[3:]
            finally:
                release.release()
                resolved.release()


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


@contextmanager
def managed_bootstrap(binary: Path, execution: Execution):
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        commands = root / "commands"
        commands.mkdir()
        (commands / "uv").symlink_to(FIXTURES / "record_uv_environment")
        (commands / "python3").symlink_to(sys.executable)
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
        environment, _ = recording_uv_environment(root)
        environment.update(without_r_environment(commands))
        environment["PYTHONPATH"] = str(root)
        environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(
            root / "inventories.json"
        )
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
                yield client, release, reached, identities
            finally:
                release.release()


@executions(DIRECT, SANDBOXED)
def test_first_declaration_replaces_blocked_bootstrap(
    binary: Path, execution: Execution
) -> list:
    with managed_bootstrap(binary, execution) as (client, release, reached, identities):
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


@executions(DIRECT, SANDBOXED)
def test_failed_declaration_preserves_bootstrap_and_reset_remains_allowed(
    binary: Path, execution: Execution
) -> list:
    with managed_bootstrap(binary, execution) as (client, release, reached, identities):
        client.initialize_and_list_tools()
        root = release.path.parent
        inventory = root / "inventories.json"
        write_uv_python_inventories(
            inventory,
            {"only-managed": [uv_python_row("3.12.12"), uv_python_row("3.11.14")]},
        )
        # The host resolver captured this path at startup. Its contents can be
        # changed through the same public inventory fixture used by version tests.
        client.send(requirements={"action": "set", "python_version": [">3", "<2"]})
        assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
        assert "Python version constraints" in last_result_text(client), (
            last_result_text(client)
        )
        assert len(identities.read_text().splitlines()) == 1
        client.send(requirements={"action": "set"})
        assert last_result_text(client) == "[prepared]"
        client.send(
            requirements={"action": "reset"},
            python="counter = 1; counter",
            timeout_ms=0,
        )
        assert last_result_text(client) == RUNNING
        reached.wait("reset starts its replacement bootstrap", timeout=600)
        workers = identities.read_text().splitlines()
        assert len(workers) == 2 and workers[0] != workers[1], workers
        release.release()
        wait_for_evaluation_output(client, "1\n", "cell after first reset")
        return client.finish()[3:]


if __name__ == "__main__":
    run_this_suite(__file__)
