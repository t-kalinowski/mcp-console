#!/usr/bin/env -S uv run --script

"""Public sans-R execution through a real OpenSSH connection."""

import json
import os
import re
import select
import shlex
import signal
import shutil
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, command, requires
from support.ssh import (
    SSH,
    client_environment,
    configure,
    localhost,
    poison_controller,
    remote_command,
)
from support.ssh_external import SANS_R_CONFIGURED, SANS_R_EXTERNAL_SSH
from support.suites import run_this_suite


@contextmanager
def ssh_session(
    binary: Path,
    execution: Execution,
    *,
    selected: bool = False,
    with_uv: bool = True,
    python_name: str = ".venv/bin/python",
    input_receipt: tuple[Path, Path] | None = None,
):
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        local = root / "controller"
        remote = root / "remote"
        remote_bin = remote / "bin"
        local.mkdir()
        remote_bin.mkdir(parents=True)
        for name, source in (("python3", sys.executable),):
            assert source is not None
            (remote_bin / name).symlink_to(source)
        if with_uv:
            uv = shutil.which("uv")
            assert uv is not None
            (remote_bin / "uv").symlink_to(uv)
        if selected:
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", remote / ".venv"],
                check=True,
            )
            if python_name == "python":
                (remote / "python").symlink_to(remote / ".venv/bin/python")
                (remote_bin / "python").symlink_to(sys.executable)
        installed = remote / "installation/bin/mcp-console"
        installed.parent.mkdir(parents=True)
        shutil.copy2(binary, installed)
        for relative in ("libexec", "share/licenses/mcp-console"):
            shutil.copytree(
                binary.parent.parent / relative, installed.parent.parent / relative
            )
        remote_environment = {
            "PATH": str(remote_bin),
            "HOME": str(remote),
            "UV_CACHE_DIR": str(remote / "uv-cache"),
        }
        config = configure(
            local,
            remote,
            remote_command(remote, installed, remote_environment),
            extends=":workspace",
        )
        if selected:
            settings = json.loads(config.read_text())
            settings["python"] = python_name
            config.write_text(json.dumps(settings))
        with localhost(root / "sshd") as controller:
            trap = poison_controller(root / "sshd", controller)
            executable = binary
            if input_receipt is not None:
                blocked, release = input_receipt
                library = build_interposer(root, "relay_stdout_read_interposer")
                controller.update(
                    {
                        "MCP_CONSOLE_TEST_RELAY_READ_MATCH": '"kind":"input_received"',
                        "MCP_CONSOLE_TEST_RELAY_READ_BLOCKED": str(blocked),
                        "MCP_CONSOLE_TEST_RELAY_READ_RELEASE": str(release),
                    }
                )
                executable = root / "controller-console"
                executable.write_text(
                    "#!/bin/sh\n"
                    'export MCP_CONSOLE_TEST_RELAY_READ_PID="$$"\n'
                    f"export {LOADER_VARIABLE}={shlex.quote(str(library))}\n"
                    f'exec {shlex.quote(str(binary))} "$@"\n'
                )
                executable.chmod(0o755)
            with McpClient(executable, execution.serve(), controller, local) as client:
                yield client, remote, local
            assert not trap.exists()


@requires(SSH, command("uv"), NATIVE_FIXTURES, PROCESS_EVENTS)
@executions(DIRECT, SANDBOXED)
def test_managed_sql_first_and_live_python(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        TemporaryDirectory() as temporary,
        closing(FifoCheckpoint.create(Path(temporary) / "input-blocked")) as blocked,
        closing(FifoCheckpoint.create(Path(temporary) / "input-release")) as release,
        ssh_session(binary, execution, input_receipt=(blocked.path, release.path)) as (
            client,
            _,
            local,
        ),
    ):
        client.initialize_and_list_tools()
        tools = client.transcript[-1]["result"]["tools"]
        assert "sql" in tools[0]["inputSchema"]["properties"]
        client.send(sql="CREATE TABLE answer AS SELECT 42 AS value")
        client.send(python="value = 42; print(value)")
        assert last_result_text(client) == "42\n"
        client.send(requirements={"python": ["six"]}, python="import six; print(value)")
        assert last_result_text(client) == "42\n"
        client.send(python="input('busy> ')")
        assert "waiting for stdin" in last_result_text(client)
        busy = client.send(requirements={"python": ["py-yaml12"]})
        assert busy["isError"] and "already evaluating" in last_result_text(client)
        client.send(requirements={"python": ["numpy"]})
        assert last_result_text(client) == "[prepared]"
        pending = client.start_send(stdin="ready\n")
        try:
            blocked.wait("SSH input receipt before controller dispatch")
            client.receive(pending)
            assert last_result_text(client) == "\n[waiting for stdin]", pending
            # Queueing input does not guarantee receipt within the 100 ms input
            # grace. Keep that partial response, then observe the ordered output
            # after releasing the actual receipt before polling for completion.
            session = next((local / ".agents/console/sessions").iterdir())
            recorded = session / "outputs/call-000004.log"
            deadline = time.monotonic() + 10
            with Events() as events:
                events.watch_file(recorded)
                events.watch_process(client.process.pid)
                release.release()
                while "'ready'\n" not in recorded.read_text():
                    assert client.process.poll() is None
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and events.wait(remaining), (
                        "controller did not capture the consumed SSH input"
                    )
            client.send()
            assert last_result_text(client) == "'ready'\n"
        finally:
            release.release()
        client.send(
            requirements={"python": ["py-yaml12"]},
            stdin="live input\n",
            python="import yaml12; assert input() == 'live input'; print(value)",
        )
        assert "42\n" in last_result_text(client)
        client.send(sql="SELECT value FROM answer")
        assert "42" in last_result_text(client)
        return client.finish()[3:]


@requires(SSH)
def test_remote_missing_uv_does_not_select_path_python(binary: Path) -> Transcript:
    with ssh_session(binary, DIRECT, with_uv=False) as (client, _, _):
        error = client.startup_error()
        assert "Python sessions without R require `uv` on PATH" in error, error
        client.stdin.close()
        assert client.process.wait(timeout=15) != 0
        assert not client.stdout.read()
        errors = client.stderr.read()
        assert "Python sessions without R require `uv` on PATH" in errors, errors
        return [{"standard_error": errors}]


@requires(SSH)
@executions(DIRECT, SANDBOXED)
def test_selected_remote_python_uses_workspace_and_no_uv(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(binary, execution, selected=True, with_uv=False) as (
        client,
        remote,
        _,
    ):
        client.initialize_and_list_tools()
        schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
        assert "r" in schema["properties"]
        client.send(sql="SELECT 42 AS value")
        assert last_result_text(client).startswith("Error: DuckDB is unavailable;")
        client.send(
            python=(
                "import os, sqlite3, sys\n"
                "assert sys.executable == os.path.join(os.getcwd(), '.venv/bin/python')\n"
                "connection = sqlite3.connect(':memory:')\n"
                "_console.sql_connection(connection)\n"
                "print('selected remote Python')"
            )
        )
        assert last_result_text(client) == "selected remote Python\n"
        client.send(sql="CREATE TABLE answer(value INTEGER)")
        client.send(sql="INSERT INTO answer VALUES (42)")
        client.send(sql="SELECT value FROM answer")
        assert "42" in last_result_text(client)
        client.send(requirements={"python": ["six"]})
        assert client.transcript[-1]["result"]["isError"]
        client.send(
            python="print(connection.execute('SELECT value FROM answer').fetchone()[0])"
        )
        assert last_result_text(client) == "42\n"
        client.send(python="input('interrupt> ')")
        assert "waiting for stdin" in last_result_text(client)
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client)
        client.send(sql="SELECT value FROM answer")
        assert "42" in last_result_text(client)
        assert (remote / ".venv/bin/python").exists()
        return client.finish()[3:]


@requires(SSH)
@executions(DIRECT, SANDBOXED)
def test_selected_remote_filename_is_relative_to_workspace(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(
        binary, execution, selected=True, with_uv=False, python_name="python"
    ) as (client, _, _):
        client.initialize_and_list_tools()
        client.send(
            python="import os, sys; assert sys.executable == os.path.join(os.getcwd(), 'python'); print('workspace filename')"
        )
        assert last_result_text(client) == "workspace filename\n", last_result_text(
            client
        )
        return client.finish()[3:]


@requires(SSH, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_automatic_import_restart_and_recording(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(binary, execution) as (client, remote, local):
        client.initialize_and_list_tools()
        client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
        client.send(
            python="import os; worker_pid = os.getpid(); steps = []; identity = object()"
        )
        client.send(
            # fmt: python
            python=code("""
                steps.append("before")
                import yaml12

                assert os.getpid() == worker_pid
                assert steps == ["before"]
                raise ValueError("after import")
                """)
        )
        assert "ValueError: after import" in last_result_text(client)
        declaration = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert "py-yaml12" in declaration["python"]
        client.send(
            # fmt: python
            python=code("""
                import subprocess, sys

                assert steps == ["before"] and os.getpid() == worker_pid
                subprocess.run([sys.executable, "-c", "import yaml12"], check=True)
                print("same worker and child interpreter")
                """)
        )
        assert last_result_text(client) == "same worker and child interpreter\n"
        client.send(sql="SELECT value FROM retained")
        assert "42" in last_result_text(client)
        client.send(
            control="restart", stdin="replacement input\n", python="print(input())"
        )
        assert "replacement input\n" in last_result_text(client)
        client.send(
            python="import yaml12; assert 'steps' not in globals(); print('retained')"
        )
        assert last_result_text(client) == "retained\n"
        client.send(python="import os; os._exit(47)")
        assert "status 47" in last_result_text(client)
        client.send(python="import yaml12; print('replacement')")
        assert last_result_text(client) == "replacement\n"
        records = client.finish()[3:]
        session = next((local / ".agents/console/sessions").iterdir())
        quarto = (session / "transcript.qmd").read_text()
        assert "yaml12" in quarto and "Files and environments remain remote" in quarto
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        assert any(
            event["event"] == "python_environment_accepted"
            and "py-yaml12" in event["packages"]
            for event in events
        )
        assert remote.is_absolute()
        return records


@requires(SSH, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_combined_additions_extensions_and_requirement_actions(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(binary, execution) as (client, remote, _):
        client.initialize_and_list_tools()
        initial = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert initial["r"] == [] and initial["duckdb"] == ["sqlite"]
        assert set(initial["python"]) == {"duckdb", "numpy", "pandas"}
        client.send(sql="SET autoinstall_known_extensions = false; LOAD sqlite")
        assert last_result_text(client) == "Success\n-------\n[0 rows]\n", (
            last_result_text(client)
        )
        client.send(python="import os; identity = object(); identity_id = id(identity)")
        client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
        client.send(
            requirements={"python": ["py-yaml12"], "duckdb": ["fts"]},
            python="import yaml12; assert id(identity) == identity_id; print('combined')",
        )
        assert last_result_text(client) == "combined\n"
        client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
        assert not client.transcript[-1]["result"]["isError"]
        client.send(sql="SELECT value FROM retained")
        assert "42" in last_result_text(client)
        assert any((remote / ".duckdb/extensions").rglob("fts.duckdb_extension"))
        current = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert (
            current["duckdb"] == ["fts", "sqlite"] and "py-yaml12" in current["python"]
        )
        client.send(requirements={"duckdb": ["tpch"]})
        assert last_result_text(client) == "[prepared]"
        client.send(sql="SET autoinstall_known_extensions = false; LOAD tpch")
        assert not client.transcript[-1]["result"]["isError"]
        client.send(
            python="assert id(identity) == identity_id; print('extension-only kept objects')"
        )
        assert last_result_text(client) == "extension-only kept objects\n"
        client.send(requirements={"duckdb": ["fts"], "python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        client.send(requirements={"action": "set", "python": ["duckdb"]})
        assert client.transcript[-1]["result"]["isError"]
        client.send(
            control="restart",
            requirements={
                "action": "set",
                "python": ["duckdb"],
                "python_version": [">=3.11"],
            },
        )
        replaced = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert replaced["python"] == ["duckdb"] and replaced["duckdb"] == []
        assert replaced["python_version"] == [">=3.11"]
        client.send(
            control="restart",
            requirements={"action": "set", "python": [], "duckdb": []},
        )
        empty = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert empty["python"] == [] and empty["duckdb"] == []
        client.send(sql="SELECT 42 AS value")
        assert last_result_text(client).startswith("Error: DuckDB is unavailable;")
        client.send(
            python="import importlib.util; assert importlib.util.find_spec('duckdb') is None"
        )
        assert (
            client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            == empty
        )
        client.send(control="restart", requirements={"action": "reset"})
        reset = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert reset == initial
        return client.finish()[3:]


@requires(SSH, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_failed_and_interrupted_remote_preparation_keeps_committed_state(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(binary, execution) as (client, remote, _):
        client.initialize_and_list_tools()
        client.send(python="identity = object(); identity_id = id(identity)")
        client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
        initial = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        remote_bin = remote / "bin"
        (remote_bin / "uv").unlink()
        fixture = Path(__file__).resolve().parents[3] / "fixtures/sans_r_uv.sh"
        uv = remote_bin / "uv"
        uv.write_text(fixture.read_text())
        uv.chmod(0o755)
        invalid = remote_bin / "invalid-python"
        invalid.write_text(fixture.read_text())
        invalid.chmod(0o755)
        (remote_bin / "invalid-inspection.json").write_text(
            json.dumps(
                {
                    "executable": str(invalid),
                    "libpython": str(remote_bin / "missing-libpython"),
                    "metadata": {
                        "base_executable": str(invalid),
                        "pythonpath": "",
                        "version": "3.12.7",
                        "version_number": "3.12",
                        "architecture": "64bit",
                        "conda": False,
                        "numpy": None,
                    },
                    **{
                        name: str(remote_bin)
                        for name in (
                            "prefix",
                            "exec_prefix",
                            "base_prefix",
                            "base_exec_prefix",
                        )
                    },
                }
            )
        )
        (remote_bin / "real-uv").symlink_to(shutil.which("uv"))
        (remote_bin / "resolutions.log").write_text("")
        (remote_bin / "mode").write_text("failure")
        with closing(FifoCheckpoint.create(remote_bin / "started")) as started:
            os.mkfifo(remote_bin / "wait")
            os.mkfifo(remote_bin / "alive")
            alive = os.open(remote_bin / "alive", os.O_RDONLY | os.O_NONBLOCK)
            try:
                failed = client.send(
                    control="restart",
                    requirements={"python": ["py-yaml12"]},
                    stdin="must not be queued\n",
                    python="raise AssertionError('failed restart ran code')",
                )
                assert failed[
                    "isError"
                ] and "fixture Python resolution failed" in last_result_text(client)
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                client.send(
                    python="assert id(identity) == identity_id; print('old objects')"
                )
                assert last_result_text(client) == "old objects\n"
                client.send(sql="SELECT value FROM retained")
                assert "42" in last_result_text(client)
                (remote_bin / "mode").write_text("interrupt")
                pending = client.start_send(
                    requirements={"python": ["py-yaml12"]},
                    python="raise AssertionError('interrupted preparation ran code')",
                )
                started.wait("remote resolver entered")
                assert os.read(alive, 1) == b"1"
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                interrupt = client.start_send(control="interrupt")
                client.receive_many([pending, interrupt])
                assert pending["result"]["isError"]
                assert "interrupted" in str(pending["result"]).lower()
                assert os.read(alive, 1) == b""
                client.send(
                    python="assert id(identity) == identity_id; print('still old worker')"
                )
                assert last_result_text(client) == "still old worker\n"
                client.send(sql="SELECT value FROM retained")
                assert "42" in last_result_text(client)
                (remote_bin / "mode").write_text("inspection")
                failed_inspection = client.send(
                    control="restart",
                    requirements={"python": ["py-yaml12"]},
                    python="raise AssertionError('failed inspection retired the worker')",
                )
                assert failed_inspection["isError"]
                assert "embedding library is missing" in last_result_text(client)
                (remote_bin / "mode").write_text("inspection-interrupt")
                inspecting = client.start_send(requirements={"python": ["py-yaml12"]})
                started.wait("remote inspector entered")
                assert os.read(alive, 1) == b"1"
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                interrupt = client.start_send(control="interrupt")
                client.receive_many([inspecting, interrupt])
                assert inspecting["result"]["isError"]
                assert "fixture Python inspection interrupted" in str(
                    inspecting["result"]
                )
                assert os.read(alive, 1) == b""
                client.send(
                    python="assert id(identity) == identity_id; print('inspection kept worker')"
                )
                assert last_result_text(client) == "inspection kept worker\n"
            finally:
                os.close(alive)
        records = client.finish()[3:]
        for record in records:
            for content in record.get("result", {}).get("content", []):
                if content.get("type") == "text":
                    content["text"] = content["text"].replace(
                        str(remote), "<execution host>"
                    )
                if (
                    content.get("type") == "text"
                    and "resolver input:" in content["text"]
                ):
                    content["text"] = re.sub(
                        r'("python": ")[^"\n]+("(?:,|\n))',
                        r"\1<execution-host Python>\2",
                        content["text"],
                    )
        return records


@requires(SSH, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_connection_loss_blocks_replacement_and_retires_native_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with ssh_session(binary, execution) as (client, remote, _):
        client.initialize_and_list_tools()
        discovery = client.send(requirements={"action": "get"})
        assert not discovery.get("isError", False), discovery
        path = remote / "worker-alive"
        gate = remote / "worker-gate"
        os.mkfifo(path)
        os.mkfifo(gate)
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            pending = client.start_send(
                python=f"import os; alive = open({str(path)!r}, 'wb', buffering=0); alive.write(b'1'); os.open({str(gate)!r}, os.O_RDONLY)",
            )
            assert select.select([descriptor], [], [], 15)[0], (
                "native worker did not enter cell"
            )
            assert os.read(descriptor, 1) == b"1"
            children = subprocess.check_output(
                ["ps", "-axo", "pid=,ppid=,command="], text=True
            )
            launchers = [
                int(pid)
                for line in children.splitlines()
                for pid, parent, command_line in [line.strip().split(maxsplit=2)]
                if int(parent) == client.process.pid and "ssh-launch" in command_line
            ]
            assert len(launchers) == 1, launchers
            os.kill(launchers[0], signal.SIGKILL)
            assert select.select([descriptor], [], [], 15)[0], (
                "remote worker survived SSH loss"
            )
            assert os.read(descriptor, 1) == b""
            client.receive(pending)
            failed = pending["result"]
            assert failed["isError"]
            assert (
                "remote retirement is unconfirmed (SSH exit is not a cleanup barrier)"
                in failed["content"][0]["text"]
            ), failed
            blocked = client.send(
                control="restart", python="raise AssertionError('unsafe replacement')"
            )
            assert (
                blocked["isError"]
                and last_result_text(client) == "[worker is shutting down]"
            ), last_result_text(client)
        finally:
            os.close(descriptor)
        client.stdin.close()
        assert not client.stdout.read(timeout=12)
        errors = client.stderr.read(timeout=12)
        assert client.process.wait(timeout=12) != 0
        records = client.transcript[3:] + [{"standard_error": errors}]
        return json.loads(json.dumps(records).replace(str(remote), "<execution host>"))


@requires(SANS_R_EXTERNAL_SSH)
@executions(DIRECT, SANDBOXED)
def test_external_r_free_execution_host(
    binary: Path, execution: Execution
) -> Transcript:
    assert SANS_R_CONFIGURED is not None
    external = SANS_R_CONFIGURED
    remote = external["target"]["transport"]["host"]
    ssh = ["ssh", "-F", external["ssh_config"], "-T", "-a", "--", remote]
    with TemporaryDirectory() as temporary:
        local = Path(temporary)
        config = local / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        settings = {
            "target": external["target"],
            "extends": ":workspace",
            "sandbox": {
                "environment": {
                    "CONSOLE_TEST_SANDBOX": str(int(execution == SANDBOXED))
                }
            },
        }
        config.write_text(json.dumps(settings))
        environment = client_environment(local, config=external["ssh_config"])
        trap = poison_controller(local, environment)
        with McpClient(binary, execution.serve(), environment, local) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            assert "Configured SSH host:" in tool["description"]
            assert "r" in tool["inputSchema"]["properties"]
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            assert not client.transcript[-1]["result"]["isError"], (
                last_result_text(client),
                client.stderr.buffer.decode(),
            )
            client.send(
                # fmt: python
                python=code("""
                    import os, subprocess, sys

                    identity = object()
                    identity_id = id(identity)
                    steps = []
                    first_tmp = os.environ["TMPDIR"]
                    print(sys.executable)
                    os.environ["HOME"] = "/worker-home-must-not-control-preparation"
                    os.environ["UV_CACHE_DIR"] = "/worker-cache-must-not-control-preparation"
                    """)
            )
            initial_python = last_result_text(client).strip()
            assert not Path(initial_python).exists(), initial_python
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<execution-host Python>\n"
            )
            client.send(
                requirements={"python": ["py-yaml12", "matplotlib"], "duckdb": ["fts"]},
                python="import yaml12; assert id(identity) == identity_id; print('combined')",
            )
            assert last_result_text(client) == "combined\n", last_result_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import errno, socket

                    if os.environ["CONSOLE_TEST_SANDBOX"] == "1":
                        try:
                            with socket.socket() as probe:
                                probe.connect(("203.0.113.1", 443))
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM), error
                        else:
                            raise AssertionError("worker network connection succeeded")
                    print("worker policy verified")
                    """)
            )
            assert last_result_text(client) == "worker policy verified\n"
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert not client.transcript[-1]["result"]["isError"]
            client.send(
                python="import matplotlib.pyplot as plt; _ = plt.plot([1, 2], [3, 4]); plt.gcf().savefig('expected.png')"
            )
            expected = subprocess.check_output(
                [
                    *ssh,
                    shlex.join(
                        ["cat", external["target"]["workspace"] + "/expected.png"]
                    ),
                ]
            )
            assert_result_content(
                client, [expected], image_reference="remote native savefig {page}"
            )
            client.send(
                # fmt: python
                python=code("""
                    steps.append("before")
                    import humanize

                    assert steps == ["before"] and id(identity) == identity_id
                    subprocess.run([sys.executable, "-c", "import yaml12, humanize"], check=True)
                    print("same cell, worker, and child selection")
                    """)
            )
            assert (
                last_result_text(client) == "same cell, worker, and child selection\n"
            )
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_result_text(client)
            client.send(python="print(os.environ['TMPDIR'])")
            first_tmp = last_result_text(client).strip()
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<execution-host temporary directory>\n"
            )
            client.send(
                control="restart",
                python="import yaml12, humanize; print('accepted restart')",
            )
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n"
                "accepted restart\n[done]"
            ), last_result_text(client)
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(python="import yaml12, humanize; print('accepted replacement')")
            assert last_result_text(client) == "accepted replacement\n"
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert {"py-yaml12", "humanize"}.issubset(declaration["python"])
            assert declaration["duckdb"] == ["fts", "sqlite"]
            client.send(
                python="import subprocess, sys; _ = subprocess.run([sys.executable, '-m', 'venv', '--without-pip', '.selected'], check=True)"
            )
            records = client.finish()[3:]
        assert not trap.exists(), "controller invoked an interpreter or resolver"
        session = next((local / ".agents/console/sessions").iterdir())
        assert (
            "Files and environments remain remote"
            in (session / "transcript.qmd").read_text()
        )
        subprocess.run([*ssh, shlex.join(["test", "!", "-e", first_tmp])], check=True)
        subprocess.run([*ssh, shlex.join(["test", "-x", initial_python])], check=True)
        settings["python"] = ".selected/bin/python"
        config.write_text(json.dumps(settings))
        with McpClient(binary, execution.serve(), environment, local) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import sqlite3; connection = sqlite3.connect(':memory:'); _console.sql_connection(connection); print('explicit remote environment')"
            )
            assert last_result_text(client) == "explicit remote environment\n"
            client.send(sql="SELECT 42 AS value")
            assert "42" in last_result_text(client)
            client.send(requirements={"python": ["humanize"]})
            assert client.transcript[-1]["result"]["isError"]
            records.extend(client.finish()[3:])
        assert not trap.exists()
        return records


if __name__ == "__main__":
    run_this_suite(__file__)
