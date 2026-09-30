#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_startup import (
    discovery_environment,
    gated_discovery,
)
from boundaries.client_server.lifecycle.test_startup import startup_fixture
from boundaries.client_server.python.test_startup import (
    isolated_python,
    selected_python,
)
from support.assertions import last_result_text
from support.allocations import AllocationProfile
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.events import Events
from support.normalization import code
from support.native import build_interposer
from support.r import r_test_environment
from support.records import Transcript
from support.resolvers import ir_run_records, recording_ir_environment
from support.requirements import R, NATIVE_FIXTURES, PROCESS_EVENTS, command, requires
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@requires(R)
def test_accepts_zero_timeout_cell_during_discovery(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with gated_discovery(binary, r_home=Path(environment["R_HOME"])) as (
        client,
        release,
    ):
        client.initialize_and_list_tools()
        client.send(
            r='started <- get0("started", ifnotfound = 0L) + 1L; started', timeout_ms=0
        )
        assert last_result_text(client) == RUNNING
        client.send(timeout_ms=0)
        assert last_result_text(client) == RUNNING
        client.send(timeout_ms=20)
        assert last_result_text(client) == RUNNING
        client.send(r="stop('second cell must not execute')", timeout_ms=0)
        assert client.transcript[-1]["result"]["isError"]
        client.request("ping")
        release.release()
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        assert last_result_text(client) == "[1] 1\n"
        client.send(r="started")
        assert last_result_text(client) == "[1] 1\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_python_before_any_send(
    binary: Path, execution: Execution
) -> Transcript:
    return blocked_python_startup(binary, execution, close=False)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_connection_closure_retires_initializing_worker(
    binary: Path, execution: Execution
) -> Transcript:
    return blocked_python_startup(binary, execution, close=True)


def blocked_python_startup(
    binary: Path, execution: Execution, *, close: bool
) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        reached = FifoCheckpoint.create(root / "reached")
        release = FifoCheckpoint.create(root / "release")
        resources.callback(reached.close)
        resources.callback(release.close)
        python, site = isolated_python(root)
        # Resolver probes use -c. Only the embedded interpreter owns this gate.
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import os
                import sys

                if sys.argv[0] != "-c":
                    with open({str(root / "pid")!r}, "w") as stream:
                        stream.write(str(os.getpid()))
                    with open({str(reached.path)!r}, "wb", buffering=0) as stream:
                        stream.write(b"1")
                    with open({str(release.path)!r}, "rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                """),
        )
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment["PATH"] = str(root)
        environment["PYTHONPATH"] = str(site)
        arguments = execution.serve(
            *(("--writable-root", str(root)) if execution == SANDBOXED else ())
        )
        with McpClient(
            binary, arguments, environment, root, response_timeout=5
        ) as client:
            # Release before connection teardown if an assertion fails.
            try:
                try:
                    reached.wait("embedded Python starts without a send")
                except AssertionError as error:
                    raise AssertionError(f"{error}; {client._diagnostics()}") from error
                client.initialize_and_list_tools()
                tools = client.transcript[-1]["result"]
                client.request("ping")
                if close:
                    worker = int((root / "pid").read_text())
                    with Events() as exits:
                        exits.watch_process(worker)
                        client.close()
                        assert worker in exits.wait(5), (
                            "initializing worker survived MCP closure"
                        )
                    return [*client.finish(), {"initializing_worker_retired": True}]
                client.send(python="import os\nos.getpid()", timeout_ms=0)
                assert last_result_text(client) == RUNNING
                release.release()
                client.send()
                assert last_result_text(client) == (root / "pid").read_text() + "\n"
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<prewarmed worker pid>\n"
                )
                assert client.request("tools/list")["result"] == tools
                client.transcript[-1]["result"] = "<unchanged from initialization>"
                client.send(python="os.getpid()")
                assert last_result_text(client) == (root / "pid").read_text() + "\n"
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<same worker pid>\n"
                )
                return client.finish()
            finally:
                release.release()


def ready_without_send(
    binary: Path, execution: Execution, *, sans_r: bool
) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        reached = FifoCheckpoint.create(root / "initialized")
        release = FifoCheckpoint.create(root / "release")
        resources.callback(reached.close)
        resources.callback(release.close)
        if sans_r:
            python, _ = isolated_python(root)
            environment = selected_python(root, python)
            environment.pop("R_HOME", None)
            environment["PATH"] = str(root)
        else:
            environment, _ = r_test_environment()
            environment["MCP_CONSOLE_LANGUAGES"] = "r"
        environment.update(
            {
                "MCP_CONSOLE_TEST_RELAY_READ_DYLIB": str(
                    build_interposer(root, "relay_stdout_read_interposer")
                ),
                "MCP_CONSOLE_TEST_RELAY_READ_MATCH": '"kind":"initialized"',
                "MCP_CONSOLE_TEST_RELAY_READ_BLOCKED": str(reached.path),
                "MCP_CONSOLE_TEST_RELAY_READ_RELEASE": str(release.path),
            }
        )
        launcher = code("""
            import os
            import sys

            os.environ["MCP_CONSOLE_TEST_RELAY_READ_PID"] = str(os.getpid())
            loader = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"
            os.environ[loader] = os.environ.pop("MCP_CONSOLE_TEST_RELAY_READ_DYLIB")
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        with McpClient(
            Path(sys.executable),
            ("-c", launcher, str(binary), *execution.serve()),
            environment,
            root,
            response_timeout=600,
        ) as client:
            try:
                # Receipt of Initialized proves actual interpreter initialization,
                # before there has been any tool call that could start a worker.
                reached.wait(
                    "default worker completed initialization without send", timeout=600
                )
                client.initialize_and_list_tools()
                tools = client.transcript[-1]["result"]
                if not sans_r:
                    client.transcript[-1]["result"] = "<configured R-only schema>"
                client.request("ping")
                release.release()
                client.send(requirements={"action": "get"})
                cell = (
                    {"python": "import os; worker_pid = os.getpid(); worker_pid"}
                    if sans_r
                    else {"r": "worker_pid <- Sys.getpid(); worker_pid"}
                )
                client.send(**cell)
                identity = last_result_text(client)
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<prewarmed worker pid>\n"
                )
                client.send(
                    **({"python": "os.getpid()"} if sans_r else {"r": "Sys.getpid()"})
                )
                assert last_result_text(client) == identity
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<same worker pid>\n"
                )
                assert client.request("tools/list")["result"] == tools
                client.transcript[-1]["result"] = "<unchanged from initialization>"
                return client.finish()
            finally:
                release.release()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_sans_r_worker_is_ready_without_send(
    binary: Path, execution: Execution
) -> Transcript:
    return ready_without_send(binary, execution, sans_r=True)


@executions(DIRECT, SANDBOXED)
@requires(R, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_r_worker_is_ready_without_send(
    binary: Path, execution: Execution
) -> Transcript:
    return ready_without_send(binary, execution, sans_r=False)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_early_replacement_requirements_withholds_cell_until_prepared(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        fixture.wait_for_resolver()
        client = fixture.client
        client.initialize_and_list_tools()
        client.request("ping")
        client.send(
            python='import importlib.util; assert importlib.util.find_spec("numpy") is None; counter = 1; counter',
            requirements={"action": "set"},
            timeout_ms=0,
        )
        assert last_result_text(client) == RUNNING
        client.send(python="counter += 1", timeout_ms=20)
        assert client.transcript[-1]["result"]["isError"]
        client.send(timeout_ms=20)
        assert last_result_text(client) == RUNNING
        fixture.release.release()
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        assert last_result_text(client) == "1\n", last_result_text(client)
        client.send(python="counter")
        assert last_result_text(client) == "1\n"
        fixture.wait_for_resolver_exit()
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R, command("ir"), command("uv"))
def test_early_requirements_select_candidate_before_default_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    r_environment, _ = r_test_environment()
    with (
        ExitStack() as resources,
        discovery_environment(r_home=Path(r_environment["R_HOME"])) as (
            discovery,
            reached,
            release,
            alive,
        ),
    ):
        root = reached.path.parent
        environment, record = recording_ir_environment(
            root, fail_requirement="tidyverse"
        )
        environment.pop("R_HOME", None)
        environment["PATH"] = os.pathsep.join((discovery["PATH"], environment["PATH"]))
        prepared = FifoCheckpoint.create(root / "prepared")
        proceed = FifoCheckpoint.create(root / "proceed")
        resources.callback(prepared.close)
        resources.callback(proceed.close)
        environment.update(
            {
                "MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT": "DBI",
                "MCP_CONSOLE_TEST_IR_STARTED": str(prepared.path),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(proceed.path),
            }
        )
        with McpClient(
            binary, execution.serve(), environment, root, response_timeout=600
        ) as client:
            reached.wait("runtime discovery is blocked")
            assert os.read(alive, 1) == b"1"
            client.initialize_and_list_tools()
            pending = client.start_send(timeout_ms=600_000)
            client.request("ping")
            client.send(r="42L", requirements={"action": "set"}, timeout_ms=0)
            assert last_result_text(client) == RUNNING
            release.release()
            try:
                prepared.wait("initial polling does not block candidate preparation")
                proceed.release()
                client.receive(pending)
                assert pending["result"]["content"] == [
                    {"type": "text", "text": "[1] 42\n"}
                ], pending
                assert not pending["result"]["isError"]
                assert not any(
                    "tidyverse" in call["arguments"] for call in ir_run_records(record)
                )
                return client.finish()
            finally:
                proceed.release()


@executions(DIRECT, SANDBOXED)
def test_idle_stdin_preserves_used_worker_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        initial = client.send(requirements={"action": "get"})["structuredContent"]
        client.transcript[-1]["result"] = "<committed default manifest>"
        client.send(stdin="retained input\n")
        changed = client.send(requirements={"action": "set"})
        assert changed["isError"] is True, changed
        assert "explicit restart" in str(changed), changed
        assert (
            client.send(requirements={"action": "get"})["structuredContent"] == initial
        )
        client.transcript[-1]["result"] = "<unchanged default manifest>"
        client.send(python="input()")
        assert last_result_text(client) == (
            "[input requested: \"\"]\n'retained input'\n"
        ), last_result_text(client)
        return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
@requires(R, PROCESS_EVENTS, command("ir"), command("uv"))
def test_deferred_requirements_cell_allows_startup_input(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        site = Path(directory)
        (site / "sitecustomize.py").write_text(
            code("""
            import os
            import sys

            if sys.argv[0] != "-c" and os.environ.get(
                "R_SESSION_INITIALIZED", ""
            ).startswith(f"PID={os.getpid()}:"):
                assert input("warmup> ") == "warmup input"
            """)
        )
        with startup_fixture(
            binary,
            execution,
            phase="none",
            server_environment={
                "PYTHONPATH": str(site),
                "RETICULATE_PYTHONPATH": str(site),
            },
        ) as fixture:
            client = fixture.client
            client.response_timeout = 600
            client.initialize_and_list_tools()
            client.send(timeout_ms=600_000)
            assert last_result_text(client) == (
                '[input requested: "warmup> "]\n[waiting for stdin]'
            ), last_result_text(client)
            client.send(
                r='cat(readLines("stdin", n = 1L)); 42L',
                requirements={"python": ["py-yaml12"]},
                stdin="cell input\n",
                timeout_ms=0,
            )
            assert not client.transcript[-1]["result"]["isError"]
            client.send(timeout_ms=0)
            assert last_result_text(client) == "\n[waiting for stdin]", (
                last_result_text(client)
            )
            client.send(stdin="warmup input\n", timeout_ms=0)
            client.send(timeout_ms=600_000)
            assert "cell input[1] 42" in last_result_text(client), last_result_text(
                client
            )
            assert not client.transcript[-1]["result"]["isError"]
            return client.finish()[3:]


@requires(NATIVE_FIXTURES)
def test_failed_discovery_discards_pending_recording(binary: Path) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as directory,
        closing(AllocationProfile(Path(directory))) as profile,
        discovery_environment() as (environment, reached, release, alive),
    ):
        environment.update(profile.environment)
        with McpClient(binary, DIRECT.serve(), environment) as client:
            reached.wait("runtime discovery is blocked")
            assert os.read(alive, 1) == b"1"
            client.initialize_and_list_tools()
            release.release()
            failure = client.send(r="stop('must not execute')")
            assert failure["isError"] is True, failure
            profile.start()
            for _ in range(1024):
                assert client.send(r="stop('must not execute')") == failure
            client.request("ping")
            _, largest = profile.stop()
            # No growing vector of requests survives a retained startup failure.
            assert largest <= 256 * 1024, largest
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            assert "fixture R discovery failed" in stderr, stderr
            return [{"failed_send_count": 1024, "result": failure}, {"stderr": stderr}]


@executions(DIRECT, SANDBOXED)
@requires(R, PROCESS_EVENTS, command("ir"), command("uv"))
def test_interrupt_stdin_preserves_used_default(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        site = Path(directory)
        (site / "sitecustomize.py").write_text(
            code("""
            import os
            import sys

            if sys.argv[0] != "-c" and os.environ.get(
                "R_SESSION_INITIALIZED", ""
            ).startswith(f"PID={os.getpid()}:"):
                input("warmup> ")
            """)
        )
        with startup_fixture(
            binary,
            execution,
            phase="none",
            server_environment={
                "PYTHONPATH": str(site),
                "RETICULATE_PYTHONPATH": str(site),
            },
        ) as fixture:
            client = fixture.client
            client.response_timeout = 600
            client.initialize_and_list_tools()
            client.send(timeout_ms=600_000)
            assert last_result_text(client) == (
                '[input requested: "warmup> "]\n[waiting for stdin]'
            ), last_result_text(client)
            client.send(control="interrupt", stdin="retained input\n", timeout_ms=0)
            assert not client.send(requirements={"action": "get"})["isError"]
            changed = client.send(requirements={"action": "set"})
            assert changed["isError"] is True, changed
            assert "explicit restart" in str(changed), changed
            return client.finish()[-1:]


if __name__ == "__main__":
    run_this_suite(__file__)
