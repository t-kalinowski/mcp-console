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
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    capture_process_identity,
    child_process_identities,
    host_process_id,
    kill_processes,
    live_processes,
)
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.r import r_test_environment
from support.records import Transcript
from support.resolvers import ir_run_records, recording_ir_environment
from support.requirements import R, NATIVE_FIXTURES, PROCESS_EVENTS, command, requires
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@requires(NATIVE_FIXTURES)
def test_connection_closure_joins_preparation_owner(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, _ = isolated_python(root)
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment.update(
            {
                "PATH": str(root),
                LOADER_VARIABLE: str(
                    build_interposer(root, "preparation_reap_interposer")
                ),
                "MCP_CONSOLE_TEST_REAP_PID": str(root / "resolver-pid"),
                "MCP_CONSOLE_TEST_REAP_DONE": str(root / "reaped"),
            }
        )
        with McpClient(binary, DIRECT.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.send(python="42")
            assert last_result_text(client) == "42\n"
            client.finish()
            assert (root / "resolver-pid").exists(), (
                "resolver did not acknowledge closure"
            )
            assert (root / "reaped").exists(), (
                "server exited before reaping preparation"
            )
            return [{"preparation_owner_joined_before_server_exit": True}]


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_connection_closure_reaps_stalled_preparation_within_shutdown_budget(
    binary: Path,
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as temporary,
        ExitStack() as resources,
    ):
        root = Path(temporary).resolve()
        blocked = resources.enter_context(
            closing(FifoCheckpoint.create(root / "blocked-close"))
        )
        python, _ = isolated_python(root)
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment.update(
            {
                "PATH": str(root),
                LOADER_VARIABLE: str(
                    build_interposer(root, "preparation_reap_interposer")
                ),
                "MCP_CONSOLE_TEST_REAP_PID": str(root / "resolver-pid"),
                "MCP_CONSOLE_TEST_REAP_DONE": str(root / "reaped"),
                "MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE": str(blocked.path),
            }
        )
        identity = None
        with McpClient(binary, DIRECT.serve(), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                client.expect("42\n", python="42")
                client.stdin.close()
                blocked.wait("preparation received Close and remains alive")
                identity = capture_process_identity(
                    int((root / "resolver-pid").read_text())
                )
                _, errors = client.finish_with_standard_error(expected_exit_status=1)
                assert (
                    errors == "local resolver setup or retirement deadline exceeded\n"
                )
                assert (root / "reaped").exists(), "preparation was not reaped"
                assert not live_processes([identity]), (
                    "preparation survived server exit"
                )
                return [
                    {
                        "stalled_preparation_reaped_before_server_exit": True,
                        "stderr": errors,
                    }
                ]
            finally:
                if identity is not None:
                    kill_processes([identity])


@executions(DIRECT, SANDBOXED)
def test_invalid_early_cell_does_not_poison_default_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as temporary,
        ExitStack() as resources,
    ):
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "probe"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import sys
                from pathlib import Path

                if sys.argv[0] == "-c":
                    with Path({str(reached.path)!r}).open("wb", buffering=0) as reached:
                        assert reached.write(b"1") == 1
                    with Path({str(release.path)!r}).open("rb", buffering=0) as release:
                        assert release.read(1) == b"1"
                """),
        )
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment["PATH"] = str(root)
        with McpClient(binary, execution.serve(), environment, root) as client:
            try:
                reached.wait("selected Python inspection is blocked")
                client.initialize_and_list_tools()
                client.send(
                    r="stop('must not execute')",
                    requirements={"python": ["six"]},
                    timeout_ms=0,
                )
                assert last_result_text(client) == RUNNING
                release.release()
                failure = client.send()
                assert failure["isError"]
                assert (
                    last_result_text(client)
                    == "R cells are unavailable in Python sessions without R"
                )
                client.send(python="answer = 42; answer")
                assert last_result_text(client) == "42\n", client.transcript[-1]
                return client.finish()[3:]
            finally:
                release.release()


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


def ready_without_send(
    binary: Path, execution: Execution, *, sans_r: bool, close: bool = False
) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        reached = FifoCheckpoint.create(root / "ready")
        release = FifoCheckpoint.create(root / "release")
        resources.callback(reached.close)
        resources.callback(release.close)
        if sans_r:
            python, site = isolated_python(root)
            python_started = FifoCheckpoint.create(root / "python-ready")
            resources.callback(python_started.close)
            (site / "sitecustomize.py").write_text(
                # fmt: python
                code(f"""
                    import os
                    import sys
                    from pathlib import Path

                    if sys.argv[0] != "-c":
                        Path({str(root / "python-started")!r}).write_text(str(os.getpid()))
                        with Path({str(python_started.path)!r}).open("wb", buffering=0) as ready:
                            assert ready.write(b"1") == 1
                    """),
            )
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
                "MCP_CONSOLE_TEST_RELAY_READ_MATCH": '"kind":"ready"',
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
            (
                "-c",
                launcher,
                str(binary),
                *execution.serve(
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ())
                ),
            ),
            environment,
            root,
            response_timeout=600,
        ) as client:
            try:
                # The relay reports Ready only after launching the real worker.
                reached.wait("default worker launched without send", timeout=600)
                owner = capture_process_identity(client.process.pid)
                descendants = []
                pending = [owner]
                while pending:
                    children = child_process_identities(pending.pop())
                    descendants.extend(children)
                    pending.extend(children)
                client.initialize_and_list_tools()
                tools = client.transcript[-1]["result"]
                if not sans_r:
                    client.transcript[-1]["result"] = "<configured R-only schema>"
                client.request("ping")
                if close:
                    try:
                        client.close()
                        survivors = live_processes(descendants)
                        assert not survivors, (
                            f"prelaunch resources survived MCP closure: {survivors}"
                        )
                        return [*client.finish(), {"prelaunch_resources_retired": True}]
                    finally:
                        kill_processes(descendants)
                release.release()
                client.send(requirements={"action": "get"})
                if sans_r:
                    python_started.wait("prelaunch initialized Python without a cell")
                cell = (
                    {"python": "import os; worker_pid = os.getpid(); worker_pid"}
                    if sans_r
                    else {"r": "worker_pid <- Sys.getpid(); worker_pid"}
                )
                client.send(**cell)
                identity = last_result_text(client)
                worker_pid = int(identity.removeprefix("[1] "))
                assert host_process_id(worker_pid, owner[0]) in {
                    identity[0] for identity in descendants
                }, "first send replaced the already launched worker"
                if sans_r:
                    assert identity == (root / "python-started").read_text() + "\n"
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
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_connection_closure_retires_prelaunch_resources(
    binary: Path, execution: Execution
) -> Transcript:
    return ready_without_send(binary, execution, sans_r=True, close=True)


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
@requires(R, NATIVE_FIXTURES, PROCESS_EVENTS, command("ir"), command("uv"))
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
        write_reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "write-reached"))
        )
        write_release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "write-release"))
        )
        evaluated = root / "evaluated"
        environment.update(
            {
                "MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT": "DBI",
                "MCP_CONSOLE_TEST_IR_STARTED": str(prepared.path),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(proceed.path),
                LOADER_VARIABLE: str(
                    build_interposer(root, "response_write_interposer")
                ),
                "MCP_CONSOLE_TEST_RESPONSE_WRITE_REACHED": str(write_reached.path),
                "MCP_CONSOLE_TEST_RESPONSE_WRITE_RELEASE": str(write_release.path),
                "MCP_CONSOLE_TEST_RESPONSE_WRITE_MATCH": "[running; poll with an empty send]",
                "MCP_CONSOLE_TEST_RESPONSE_WRITE_COMPLETE": "1",
                "MCP_CONSOLE_TEST_RESPONSE_WRITE_ARMED": str(root / "write-armed"),
                "MCP_CONSOLE_TEST_EVALUATED": str(evaluated),
            }
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
            reached.wait("runtime discovery is blocked")
            assert os.read(alive, 1) == b"1"
            client.initialize_and_list_tools()
            pending = client.start_send(timeout_ms=600_000)
            client.request("ping")
            (root / "write-armed").touch()
            try:
                submitted = client.start_send(
                    r='cat("1", file = Sys.getenv("MCP_CONSOLE_TEST_EVALUATED")); 42L',
                    requirements={"action": "set"},
                    timeout_ms=0,
                )
                write_reached.wait(
                    "running response is visible before its write settles"
                )
                client.receive(submitted)
                assert last_result_text(client) == RUNNING
                release.release()
                prepared.wait("initial polling does not block candidate preparation")
                proceed.release()
                wait_for_path(evaluated, "accepted cell ran", client=client)
                write_release.release()
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
                write_release.release()
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


if __name__ == "__main__":
    run_this_suite(__file__)
