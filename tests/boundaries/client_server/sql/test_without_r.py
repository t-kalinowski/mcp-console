"""Public MCP SQL coverage with no R executable visible to the local server."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code, normalize_python_resolution_error
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, requires


def environment(path: Path) -> dict[str, str]:
    env = dict(os.environ, PATH=str(path))
    for name in (
        "R_HOME",
        "R_LIBS",
        "R_LIBS_USER",
        "RETICULATE_PYTHON",
        "RETICULATE_UV",
    ):
        env.pop(name, None)
    return env


def installed_binary(binary: Path, root: Path) -> Path:
    prefix = root / "installation"
    (prefix / "bin").mkdir(parents=True)
    installed = prefix / "bin/mcp-console"
    shutil.copy2(binary, installed)
    source_prefix = binary.parent.parent
    for relative in ("libexec", "share/licenses/mcp-console"):
        shutil.copytree(source_prefix / relative, prefix / relative)
    return installed


@executions(DIRECT, SANDBOXED)
def test_sqlite_is_available_by_default(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        with sqlite3.connect(root / "audit.sqlite") as database:
            database.execute("CREATE TABLE events (payload TEXT)")
            database.execute("INSERT INTO events VALUES (?)", ('{"answer":42}',))
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "sqlite"
            ]
            assert list(
                (home / ".duckdb/extensions").glob(
                    "v*/**/sqlite_scanner.duckdb_extension"
                )
            )
            client.send(sql="SET autoinstall_known_extensions = false")
            client.send(sql="ATTACH 'audit.sqlite' AS audit (TYPE sqlite, READ_ONLY)")
            client.send(sql="SELECT payload->>'$.answer' AS answer FROM audit.events")
            assert "42" in last_tool_text(client), last_tool_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_managed_python_requires_home_for_default_extensions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        env = dict(environment(root), UV_CACHE_DIR=str(root / "uv-cache"))
        env.pop("HOME", None)
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            env,
            record_in_project=False,
        ) as client:
            client.process.wait(timeout=60)
            diagnostic = client.stderr.read()
            assert client.process.returncode != 0
            assert (
                "DuckDB extension preparation requires an absolute HOME" in diagnostic
            )
            return [{"stderr": diagnostic}]


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_default_extension_failure_preserves_close_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        marker = root / "closing"
        env = dict(
            environment(root),
            UV_CACHE_DIR=str(root / "uv-cache"),
            MCP_CONSOLE_TEST_CLOSE_MARKER=str(marker),
        )
        env[LOADER_VARIABLE] = str(build_interposer(root, "preparation_close_failure"))
        # Python selection succeeds, but default extension preparation needs HOME.
        env.pop("HOME", None)
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            env,
            record_in_project=False,
        ) as client:
            assert client.process.wait(timeout=60) != 0
            assert not client.stdout.read()
            diagnostic = client.stderr.read()
            assert marker.exists(), ("resolver did not receive Close", diagnostic)
            assert diagnostic.strip() == (
                "DuckDB extension preparation requires an absolute HOME at server startup; "
                "resolver input closed"
            ), diagnostic
            return [{"stderr": diagnostic}]


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_prepares_extension_before_first_worker_and_loads_from_cache(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        cache = home / ".duckdb/extensions"
        assert not cache.exists()
        shadow = root / "duckdb.py"
        shadow.write_text(
            "raise RuntimeError('workspace DuckDB shadow was imported')\n"
        )
        env = dict(environment(root), HOME=str(home), PYTHONPATH=str(root))
        if execution == DIRECT:
            env[LOADER_VARIABLE] = str(build_interposer(root, "deny_worker_connect"))
            env["MCP_CONSOLE_TEST_DENY_WORKER_NETWORK"] = "1"
        with McpClient(
            installed_binary(binary, root), execution.serve(), env
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(requirements={"duckdb": ["fts"]})
            assert not result.get("isError"), result
            (extension,) = cache.glob("v*/**/fts.duckdb_extension")
            shadow.unlink()
            client.send(
                # fmt: python
                python=code("""
                    import errno
                    import socket

                    try:
                        # Linux may deny socket creation before connect is reached.
                        with socket.socket() as probe:
                            probe.settimeout(1)
                            probe.connect(("203.0.113.1", 443))
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM), error
                    else:
                        raise AssertionError("worker network connection succeeded")
                    print("worker network denied")
                    """)
            )
            assert last_tool_text(client) == "worker network denied\n", last_tool_text(
                client
            )
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                sql="SELECT installed, loaded FROM duckdb_extensions() WHERE extension_name = 'fts'"
            )
            assert "true" in last_tool_text(client).lower()
            client.send(python="import os; print(os.environ['TMPDIR'])")
            first_temporary = Path(last_tool_text(client).strip())
            (root / "uv").unlink()
            client.send(
                control="restart",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert "Error:" not in last_tool_text(client)
            assert not first_temporary.exists()
            assert extension.is_file(), "restart removed the shared extension cache"
            client.send(python="import os; print(os.environ['TMPDIR'])")
            second_temporary = Path(last_tool_text(client).strip())
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            assert not second_temporary.exists()
            assert extension.is_file(), (
                "crash replacement removed the shared extension cache"
            )
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "fts",
                "sqlite",
            ]
            return _replace_paths(
                client.finish()[3:], [first_temporary, second_temporary]
            )


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_adds_extensions_to_idle_worker_without_losing_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        env = dict(environment(root), HOME=str(home))
        if execution == DIRECT:
            env[LOADER_VARIABLE] = str(build_interposer(root, "deny_worker_connect"))
            env["MCP_CONSOLE_TEST_DENY_WORKER_NETWORK"] = "1"
        with McpClient(
            installed_binary(binary, root), execution.serve(), env
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sqlite3
                    import sys

                    identity = object()
                    identity_id = id(identity)
                    worker_pid = os.getpid()
                    interpreter = sys.executable
                    managed = sql_connection()
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (17)")
                    console_sql_connection(selected)
                    print("state ready")
                    """)
            )
            assert last_tool_text(client) == "state ready\n"
            prepared = client.send(
                requirements={"duckdb": ["fts"], "python": ["duckdb"]}
            )
            assert not prepared.get("isError"), prepared
            assert (
                len(
                    list(
                        (home / ".duckdb/extensions").glob("v*/**/fts.duckdb_extension")
                    )
                )
                == 1
            )
            client.send(
                # fmt: python
                python=code("""
                    assert os.getpid() == worker_pid
                    assert sys.executable == interpreter
                    assert id(identity) == identity_id
                    assert sql_connection() is selected
                    assert managed.execute("SELECT value FROM retained").fetchone() == (42,)
                    assert selected.execute("SELECT value FROM chosen").fetchone() == (17,)
                    console_sql_connection(None)
                    assert sql_connection() is managed
                    print("state retained")
                    """)
            )
            assert last_tool_text(client) == "state retained\n"
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                requirements={"duckdb": ["excel"]},
                sql="SET autoinstall_known_extensions = false; LOAD excel; SELECT value FROM retained",
            )
            assert "42" in last_tool_text(client)
            client.send(
                requirements={"duckdb": ["json"]},
                python="assert os.getpid() == worker_pid and id(identity) == identity_id; print('Python cell retained')",
            )
            assert last_tool_text(client) == "Python cell retained\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "excel",
                "fts",
                "json",
                "sqlite",
            ]
            client.send(
                control="restart",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert "Error:" not in last_tool_text(client)
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD excel")
            assert "Error:" not in last_tool_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_combines_python_and_extension_candidates_across_duckdb_versions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        cache = home / ".duckdb/extensions"
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
        ) as client:
            client.initialize_and_list_tools()
            first = client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.4.4", "six"],
                    "duckdb": ["fts"],
                },
                python="import duckdb, six; assert duckdb.__version__ == '1.4.4'; print('first candidate')",
            )
            assert not first.get("isError"), first
            assert last_tool_text(client) == "first candidate\n"
            assert len(list(cache.glob("v1.4.4/**/fts.duckdb_extension"))) == 1
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"] == {
                "r": [],
                "python": ["duckdb==1.4.4", "six"],
                "duckdb": ["fts"],
                "python_version": [],
                "exclude_newer": None,
            }
            second = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.5", "six"],
                    "duckdb": ["fts"],
                },
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert not second.get("isError"), second
            assert "Error:" not in last_tool_text(client)
            assert len(list(cache.glob("v1.5.5/**/fts.duckdb_extension"))) == 1
            client.send(python="import duckdb, six; print(duckdb.__version__)")
            assert last_tool_text(client) == "1.5.5\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["six"], "duckdb": []},
                python="sentinel = object(); original = sentinel",
            )
            added = client.send(
                requirements={"python": ["duckdb==1.5.5"], "duckdb": ["fts"]},
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert not added.get("isError"), added
            assert "Error:" not in last_tool_text(client), client.transcript[-1]
            client.send(
                python="assert sentinel is original; print('candidate prepared together')"
            )
            assert last_tool_text(client) == "candidate prepared together\n"
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_combined_preparation_precedes_first_sql_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(
                requirements={"python": ["six"], "duckdb": ["fts"]},
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert not result.get("isError"), result
            assert "Error:" not in last_tool_text(client)
            client.send(python="import six; print('combined SQL preparation')")
            assert last_tool_text(client) == "combined SQL preparation\n"
            assert (
                len(
                    list(
                        (home / ".duckdb/extensions").glob("v*/**/fts.duckdb_extension")
                    )
                )
                == 1
            )
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_extension_actions_replace_and_reset_declarations(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        cache = home / ".duckdb/extensions"
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
        ) as client:
            client.initialize_and_list_tools()

            def declaration():
                inspected = client.send(requirements={"action": "get"})
                assert not inspected.get("isError"), inspected
                return inspected["structuredContent"]["requirements"]

            assert declaration()["duckdb"] == ["sqlite"]
            client.send(requirements={"duckdb": ["fts"]})
            assert declaration()["duckdb"] == ["fts", "sqlite"]
            client.send(requirements={"duckdb": ["json"]})
            assert declaration()["duckdb"] == ["fts", "json", "sqlite"]
            client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb"],
                    "duckdb": ["fts"],
                }
            )
            assert declaration()["python"] == ["duckdb"]
            assert declaration()["duckdb"] == ["fts"]
            (extension,) = cache.glob("v*/**/fts.duckdb_extension")
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                control="restart", requirements={"action": "set", "python": ["duckdb"]}
            )
            assert declaration()["duckdb"] == []
            assert extension.is_file(), (
                "removing a declaration uninstalled the extension"
            )
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(control="restart", requirements={"action": "reset"})
            assert declaration()["python"] == ["numpy", "pandas", "duckdb"]
            assert declaration()["duckdb"] == ["sqlite"]
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_failed_and_live_extension_changes_preserve_worker_and_selected_connection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        uv_cache = root / "uv-cache"
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home), UV_CACHE_DIR=str(uv_cache)),
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.5"],
                    "duckdb": ["fts"],
                }
            )
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(
                # fmt: python
                python=code("""
                    import sqlite3

                    identity = object()
                    identity_id = id(identity)
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (17)")
                    console_sql_connection(selected)
                    print("selected SQLite")
                    """)
            )
            assert last_tool_text(client) == "selected SQLite\n"
            client.send(requirements={"duckdb": ["fts"]})
            client.send(sql="SELECT value FROM chosen")
            assert "17" in last_tool_text(client)
            failed = client.send(
                requirements={"duckdb": ["not_a_real_duckdb_extension"]},
                stdin="retained input\n",
                sql="DROP TABLE retained",
            )
            assert failed.get("isError"), failed
            failure = last_result_text(client)
            assert "not_a_real_duckdb_extension" in failure
            failure = re.sub(
                r"https://duckdb\.org/docs/stable/extensions/troubleshooting\?\S+",
                "<DuckDB extension troubleshooting URL>",
                failure,
            )
            failed["content"][0]["text"] = re.sub(
                r'https?://[^"\s]+', "<DuckDB extension URL>", failure
            )
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(sql="SELECT value FROM chosen")
            assert "17" in last_tool_text(client)
            client.send(python="assert id(identity) == identity_id; print(input())")
            assert "retained input" not in last_tool_text(client)
            assert "[waiting for stdin]" in last_tool_text(client)
            client.send(stdin="fresh input\n")
            assert "fresh input" in last_tool_text(client)
            client.send(python="console_sql_connection(None)")
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            missing = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "python": ["six"],
                    "duckdb": ["fts"],
                },
                python="identity = None",
            )
            assert missing.get("isError"), missing
            assert "include duckdb in requirements.python" in last_result_text(client)
            client.send(
                python="assert id(identity) == identity_id; print('old worker intact')"
            )
            assert last_tool_text(client) == "old worker intact\n"
            live = client.send(
                requirements={
                    "duckdb": ["json"],
                    "python": ["absent-fixture-distribution"],
                },
                python="identity = None",
            )
            assert live.get("isError"), live
            assert "absent-fixture-distribution" in last_result_text(client)
            live["content"][0]["text"], paths = re.subn(
                rf'"python": "{re.escape(str(uv_cache))}/archive-v0/[^"]+/bin/python"',
                '"python": "<selected Python>"',
                last_result_text(client),
                count=1,
            )
            assert paths == 1, last_result_text(client)
            for requirements in (
                {"duckdb": ["json"], "python_version": [">=3.11"]},
                {"duckdb": ["json"], "exclude_newer": "2026-01-01"},
                {
                    "action": "set",
                    "python": ["duckdb==1.5.5"],
                    "duckdb": ["fts", "json"],
                },
                {"action": "reset"},
            ):
                denied = client.send(requirements=requirements)
                assert denied.get("isError"), denied
                assert 'control="restart"' in last_result_text(client)
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(
                python="assert id(identity) == identity_id; print('still intact')"
            )
            assert last_tool_text(client) == "still intact\n"
            unchanged = client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.5"],
                    "duckdb": ["fts"],
                }
            )
            assert not unchanged.get("isError"), unchanged
            client.send(
                requirements={"duckdb": ["fts"]},
                python="assert id(identity) == identity_id; print('no-op')",
            )
            assert last_tool_text(client) == "no-op\n"
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_live_extension_additions_require_an_idle_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"duckdb": ["json"]})
            client.send(
                python="identity = object(); identity_id = id(identity); answer = input('answer> ')",
            )
            assert "[waiting for stdin]" in last_tool_text(client)
            busy = client.send(
                requirements={"duckdb": ["fts"]},
                python="identity = None",
            )
            assert busy.get("isError"), busy
            assert "already evaluating a cell" in last_result_text(client)
            retained = client.send(requirements={"duckdb": ["json"]})
            assert not retained.get("isError"), retained
            client.send(stdin="kept\n")
            client.send(
                python="assert id(identity) == identity_id and answer == 'kept'; print('input retained')"
            )
            assert last_tool_text(client) == "input retained\n"

            client.send(
                python="import pdb; pdb.set_trace(); print('debugger continued')"
            )
            assert "[waiting for stdin]" in last_tool_text(client)
            busy = client.send(requirements={"duckdb": ["fts"]})
            assert busy.get("isError"), busy
            assert "already evaluating a cell" in last_result_text(client)
            denied = client.send(
                control="interrupt",
                requirements={"duckdb": ["json"]},
                python="identity = None",
            )
            assert denied.get("isError"), denied
            client.send(stdin="continue\n")
            assert "debugger continued" in last_result_text(client)
            client.send(
                python="assert id(identity) == identity_id; print('still live')"
            )
            assert last_tool_text(client) == "still live\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "json",
                "sqlite",
            ]
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_interrupts_extension_preparation_before_worker_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        started = FifoCheckpoint.create(root / "started")
        release = FifoCheckpoint.create(root / "release")
        env = dict(
            environment(root),
            HOME=str(home),
            UV_CACHE_DIR=str(root / "uv-cache"),
            MCP_CONSOLE_TEST_DUCKDB_INTERRUPT_ROOT=str(root),
        )
        try:
            with McpClient(
                installed_binary(binary, root), execution.serve(), env
            ) as client:
                client.initialize_and_list_tools()
                client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
                client.send(
                    python="import os, sysconfig; site = sysconfig.get_paths()['purelib']; identity = object(); identity_id = id(identity); pid = os.getpid(); print(site)"
                )
                site = Path(last_tool_text(client).strip())
                hook = site / "sitecustomize.py"
                hook.write_text(
                    code("""
                        import os
                        import signal
                        import sys
                        from pathlib import Path

                        if sys.flags.isolated:
                            signal.signal(signal.SIGINT, lambda *_: os._exit(130))
                            root = Path(os.environ["MCP_CONSOLE_TEST_DUCKDB_INTERRUPT_ROOT"])
                            with (root / "started").open("wb", buffering=0) as marker:
                                marker.write(b"1")
                            with (root / "release").open("rb", buffering=0) as gate:
                                gate.read(1)
                        """)
                )
                try:
                    pending = client.start_send(
                        requirements={"duckdb": ["fts"]},
                        sql="DROP TABLE retained",
                    )
                    started.wait("Python DuckDB resolver entered")
                    inspected = client.send(requirements={"action": "get"})
                    assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                        "sqlite"
                    ]
                    interrupt = client.start_send(control="interrupt")
                    client.receive_many([pending, interrupt])
                    assert pending["result"].get("isError"), pending
                    assert "exit status: 130" in str(pending["result"]), pending
                    assert not interrupt["result"].get("isError"), interrupt
                finally:
                    release.release()
                    hook.unlink()
                inspected = client.send(requirements={"action": "get"})
                assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                    "sqlite"
                ]
                client.send(sql="SELECT value FROM retained")
                assert "42" in last_tool_text(client)
                client.send(
                    python="assert id(identity) == identity_id and os.getpid() == pid; print('still live')"
                )
                assert last_tool_text(client) == "still live\n"
                records = client.finish()[3:]
                for record in records:
                    for content in record.get("result", {}).get("content", []):
                        if content.get("type") == "text":
                            text = content["text"].replace(str(root), "<fixture>")
                            content["text"] = re.sub(
                                r"<fixture>/uv-cache/archive-v0/[^/]+",
                                "<managed Python>",
                                text,
                            )
                return records
        finally:
            started.close()
            release.close()


@executions(DIRECT, SANDBOXED)
def test_sql_is_the_first_cell(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root), execution.serve(), environment(root)
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(sql="SELECT 42 AS answer")
            assert not result.get("isError"), result
            assert "42" in last_tool_text(client)
            client.send(sql="CREATE TABLE retained AS SELECT 7 AS value")
            assert "Error:" not in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import pandas as pd

                    managed = sql_connection()
                    assert managed.execute("SELECT value FROM retained").fetchone() == (7,)
                    frame = pd.DataFrame({"value": [3, 4]})
                    managed.register("registered", frame)
                    unregistered = pd.DataFrame({"value": [99]})
                    print("shared connection")
                    """)
            )
            assert last_tool_text(client) == "shared connection\n"
            client.send(sql="SELECT sum(value) AS total FROM registered")
            assert "7" in last_tool_text(client)
            client.send(sql="SELECT * FROM unregistered")
            assert "Error:" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import sqlite3

                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (11)")
                    console_sql_connection(selected)
                    assert sql_connection() is selected
                    print("selected SQLite")
                    """)
            )
            assert last_tool_text(client) == "selected SQLite\n"
            client.send(sql="SELECT value FROM chosen")
            assert "11" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    console_sql_connection(None)
                    assert sql_connection() is managed
                    assert selected.execute("SELECT value FROM chosen").fetchone() == (11,)
                    print("managed restored; SQLite remains open")
                    """)
            )
            assert last_tool_text(client) == "managed restored; SQLite remains open\n"
            client.send(sql="SELECT value FROM retained")
            assert "7" in last_tool_text(client)
            client.send(sql="SELECT i FROM range(100) AS values(i)")
            preview = last_tool_text(client)
            assert "[additional rows omitted]" in preview
            assert len(preview.encode()) < 12 * 1024
            client.send(sql="SELECT * FROM missing_table")
            assert "Error:" in last_tool_text(client)
            client.send(
                python="managed.execute('SELECT value FROM retained').fetchone()"
            )
            assert last_tool_text(client) == "(7,)\n"
            return client.finish()[3:]


def _replace_paths(records: Transcript, paths: list[Path]) -> Transcript:
    for record in records:
        for content in record.get("result", {}).get("content", []):
            if content.get("type") == "text":
                for index, path in enumerate(paths):
                    content["text"] = content["text"].replace(
                        str(path), f"<worker temporary {index + 1}>"
                    )
    return records


@executions(DIRECT, SANDBOXED)
def test_catalog_and_private_storage_follow_worker_lifetime(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root), execution.serve(), environment(root)
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os, sys
                    from pathlib import Path

                    first_temporary = Path(os.environ["TMPDIR"])
                    assert "duckdb" not in sys.modules
                    assert not (first_temporary / "mcp-console-duckdb").exists()
                    print(first_temporary)
                    """)
            )
            first_temporary = Path(last_tool_text(client).strip())
            client.send(sql="CREATE TABLE state AS SELECT 41 AS value")
            client.send(
                # fmt: python
                python=code("""
                    connection = sql_connection()
                    spill = Path(
                        connection.execute("SELECT current_setting('temp_directory')").fetchone()[0]
                    )
                    secrets = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    )
                    assert spill == first_temporary / "mcp-console-duckdb/spill"
                    assert secrets == first_temporary / "mcp-console-duckdb/stored-secrets"
                    assert connection.execute(
                        "SELECT current_setting('python_enable_replacements')"
                    ).fetchone() == (False,)
                    spill.mkdir(parents=True)
                    secrets.mkdir(parents=True)
                    (spill / "retired").touch()
                    (secrets / "retired").touch()
                    print("private DuckDB storage")
                    """)
            )
            assert last_tool_text(client) == "private DuckDB storage\n"
            client.send(control="restart", sql="SELECT * FROM state")
            assert "Error:" in last_tool_text(client)
            assert not first_temporary.exists(), "retired worker storage remains"
            client.send(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    second_temporary = Path(os.environ["TMPDIR"])
                    assert second_temporary != first_temporary
                    print(second_temporary)
                    """)
            )
            second_temporary = Path(last_tool_text(client).strip())
            client.send(sql="CREATE TABLE crash_state AS SELECT 42 AS value")
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SELECT * FROM crash_state")
            assert "Error:" in last_tool_text(client)
            assert not second_temporary.exists(), "crashed worker storage remains"
            client.send(python="import os; print(os.environ['TMPDIR'])")
            third_temporary = Path(last_tool_text(client).strip())
            records = client.finish()[3:]
            assert not third_temporary.exists(), "shutdown worker storage remains"
            return _replace_paths(
                records, [first_temporary, second_temporary, third_temporary]
            )


@executions(DIRECT, SANDBOXED)
def test_requirements_and_missing_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        uv = root / "uv"
        uv.write_text(
            """#!/bin/sh
for argument do
    if [ "$argument" = py-yaml12 ]; then
        echo 'fixture Python resolution failed' >&2
        exit 81
    fi
done
exec "$MCP_CONSOLE_TEST_REAL_UV" "$@"
"""
        )
        uv.chmod(0o755)
        env = dict(environment(root), MCP_CONSOLE_TEST_REAL_UV=shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root), execution.serve(), env
        ) as client:
            client.initialize_and_list_tools()

            def declaration():
                result = client.send(requirements={"action": "get"})
                assert not result.get("isError"), result
                return result["structuredContent"]["requirements"]["python"]

            assert declaration() == ["numpy", "pandas", "duckdb"]
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(python="identity = object(); identity_id = id(identity)")
            failed = client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                sql="DROP TABLE retained",
            )
            assert failed.get("isError")
            assert "fixture Python resolution failed" in last_result_text(client)
            failed["content"][0]["text"] = normalize_python_resolution_error(
                failed["content"][0]["text"], "fixture Python resolution failed"
            )
            assert declaration() == ["numpy", "pandas", "duckdb"]
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            client.send(python="assert id(identity) == identity_id; print('retained')")
            assert last_tool_text(client) == "retained\n"

            client.send(control="restart", requirements={"action": "set"})
            assert declaration() == []
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sqlite3

                    assert importlib.util.find_spec("duckdb") is None
                    selected = sqlite3.connect(":memory:")
                    console_sql_connection(selected)
                    print("Python remains available")
                    """)
            )
            assert last_tool_text(client) == "Python remains available\n"
            client.send(sql="SELECT 7 AS custom_value")
            assert "7" in last_tool_text(client)
            client.send(
                python="console_sql_connection(None); selected.execute('SELECT 1').fetchone()"
            )
            assert last_tool_text(client) == "(1,)\n"
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            assert declaration() == []
            client.send(control="restart", requirements={"action": "reset"})
            assert declaration() == ["numpy", "pandas", "duckdb"]
            client.send(sql="SELECT 9 AS restored")
            assert "9" in last_tool_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_selected_environment_uses_custom_connection_without_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", workspace / ".venv"],
            check=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: .venv/bin/python\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            environment(bin_dir),
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sqlite3

                    assert importlib.util.find_spec("duckdb") is None
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE custom(value INTEGER)")
                    selected.execute("INSERT INTO custom VALUES (21)")
                    console_sql_connection(selected)
                    assert sql_connection() is selected
                    print("custom connection selected")
                    """)
            )
            assert last_tool_text(client) == "custom connection selected\n"
            client.send(sql="SELECT value * 2 AS answer FROM custom")
            assert "42" in last_tool_text(client)
            client.send(
                python="console_sql_connection(None); selected.execute('SELECT value FROM custom').fetchone()"
            )
            assert last_tool_text(client) == "(21,)\n"
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_selected_environment_uses_preinstalled_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        python = workspace / ".venv/bin/python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", python.parent.parent],
            check=True,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", python, "duckdb"],
            check=True,
            capture_output=True,
        )
        home = root / "home"
        home.mkdir()
        subprocess.run(
            [
                python,
                "-I",
                "-c",
                "import duckdb; connection = duckdb.connect(':memory:'); connection.install_extension('fts')",
            ],
            check=True,
            capture_output=True,
            env=dict(os.environ, HOME=str(home)),
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: .venv/bin/python\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(bin_dir), HOME=str(home)),
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE selected_state AS SELECT 42 AS value")
            assert "Error:" not in last_tool_text(client)
            client.send(sql="SELECT value FROM selected_state")
            assert "42" in last_tool_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                python="sql_connection().execute('SELECT value FROM selected_state').fetchone()"
            )
            assert last_tool_text(client) == "(42,)\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["python"] == []
            refused = client.send(requirements={"duckdb": ["fts"]})
            assert refused.get("isError"), refused
            assert "user-selected Python environment" in last_result_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_records_managed_sql_cells(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "home"
        home.mkdir()
        (root / "uv").symlink_to(shutil.which("uv"))
        with McpClient(
            installed_binary(binary, root),
            execution.serve(),
            dict(environment(root), HOME=str(home)),
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(sql="SELECT 42 AS recorded")
            added = client.send(requirements={"duckdb": ["json"]})
            assert not added.get("isError"), added
            client.send(python='sql_connection().execute("SELECT 1").fetchone()')
            records = client.finish()[3:]
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        markdown = (session / "transcript.md").read_text()
        quarto = (session / "transcript.qmd").read_text()
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        additions = [
            event
            for event in events
            if event["event"] == "tool_call"
            and event["request"].get("arguments", {}).get("requirements")
            == {"duckdb": ["json"]}
        ]
        assert len(additions) == 1, additions
        assert any(
            event["event"] == "tool_result"
            and event["call_id"] == additions[0]["call_id"]
            for event in events
        )
        assert not any(event["event"] == "requirements_selected" for event in events)
        assert '"duckdb": [\n      "json"\n' in markdown
        assert "[prepared]" in markdown
        assert "Requirements selected" not in markdown
        assert "execute:\n  eval: false" not in quarto
        assert "```sql\nSELECT 42 AS recorded\n```" in markdown
        assert "```{sql}\nSELECT 42 AS recorded\n```" in quarto
        assert (
            """  python-packages:
    - numpy
    - pandas
    - duckdb
"""
            in quarto
        )
        assert "  packages: []\n" in quarto
        return TranscriptWithCompanions(
            records, {"qmd": quarto.replace(str(workspace.resolve()), "<workspace>")}
        )


@executions(DIRECT, SANDBOXED)
def test_language_restriction_still_disables_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        env = dict(environment(root), MCP_CONSOLE_LANGUAGES="python")
        with McpClient(
            installed_binary(binary, root), execution.serve(), env
        ) as client:
            client.initialize_and_list_tools()
            schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
            assert "sql" not in schema["properties"]
            client.send(python="retained = 42; retained")
            assert last_tool_text(client) == "42\n"
            result = client.send(sql="SELECT 1")
            assert result.get("isError")
            assert "disabled by `MCP_CONSOLE_LANGUAGES`" in last_result_text(client)
            client.send(python="retained")
            assert last_tool_text(client) == "42\n"
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_interrupt_preserves_sql_and_python_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "uv").symlink_to(shutil.which("uv"))
        env = environment(root)
        env["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"] = str(
            build_interposer(root, "python_probe_checkpoint")
        )
        client = McpClient(installed_binary(binary, root), execution.serve(), env)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(
                # fmt: python
                python=code(r"""
                    import ctypes
                    import os
                    import signal
                    from pathlib import Path

                    library = ctypes.PyDLL(os.environ["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"])
                    wait_for_interrupt = library.wait_for_probe_interrupt
                    wait_for_interrupt.argtypes = (
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_void_p,
                    )
                    wait_for_interrupt.restype = ctypes.c_int
                    started_path = Path(os.environ["TMPDIR"], "sql-interrupt-started")
                    release_path = Path(os.environ["TMPDIR"], "sql-interrupt-release")


                    class InterruptibleConnection:
                        description = (("answer",),)

                        def cursor(self):
                            return self

                        def execute(self, source):
                            if source == "WAIT":
                                wakeup_read, wakeup_write = os.pipe()
                                os.set_blocking(wakeup_write, False)
                                previous_wakeup = signal.set_wakeup_fd(wakeup_write)
                                try:
                                    with (
                                        started_path.open("wb", buffering=0) as started,
                                        release_path.open("rb", buffering=0) as release,
                                    ):
                                        assert (
                                            wait_for_interrupt(
                                                started.fileno(),
                                                release.fileno(),
                                                wakeup_read,
                                                ctypes.pythonapi.PyErr_CheckSignals,
                                            )
                                            == 0
                                        )
                                finally:
                                    signal.set_wakeup_fd(previous_wakeup)
                                    os.close(wakeup_read)
                                    os.close(wakeup_write)
                            return self

                        def fetchmany(self, size):
                            return [(7,)]


                    console_sql_connection(InterruptibleConnection())
                    print(started_path, release_path, sep="\n")
                    """)
            )
            setup = client.transcript[-1]["result"]
            paths = last_tool_text(client).splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = (
                "<SQL interrupt started>\n<SQL interrupt release>\n"
            )
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            client.send(sql="WAIT", timeout_ms=0)
            assert "[running; poll with an empty send]" in last_tool_text(client)
            started.wait("sans-R SQL entered the interrupt checkpoint")
            client.send(control="interrupt", timeout_ms=0)
            release.release()
            release = None
            client.send()
            assert "KeyboardInterrupt" in last_tool_text(client)
            client.send(
                python="console_sql_connection(None); print('Python remains usable')"
            )
            assert last_tool_text(client) == "Python remains usable\n"
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            records = client.finish()[3:]
            passed = True
            return records
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)
