"""Captured startup source selects native connections before the first SQL cell."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.assertions import assert_result_content, last_tool_text
from support.checkpoints import wait_for_worker_file
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.normalization import code
from support.progress import without_elapsed_result
from support.records import ToolResult, Transcript
from support.requirements import POSIX, PROCESS_EVENTS, R, SQL, command, requires
from support.resolvers import matplotlib_test_environment
from support.snapshots import normalize_request_ids
from boundaries.client_server.python.test_startup import isolated_python
from boundaries.client_server.server.test_no_r import no_r_environment


def configure(workspace: Path, language: str, source: str) -> Path:
    config = workspace / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"startup": {"language": language, "code": source}}))
    return config


@requires(POSIX, R, SQL, command("ps"))
@executions(DIRECT)
def test_startup_source_absent_from_exec_environments(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    marker = "captured-startup-credential-marker"
    for language in ("python", "r"):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            if language == "python":
                # fmt: python
                source = code("""
                    import sqlite3

                    native = sqlite3.connect(":memory:")
                    console_sql_connection(native)
                    """)
            else:
                # fmt: r
                source = code("""
                    native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                    console_sql_connection(native)
                    """)
            configure(workspace, language, f"# {marker}\n" + source)
            with McpClient(
                binary,
                execution.serve(),
                os.environ | {"TMPDIR": str(workspace)},
                workspace,
            ) as client:
                client.initialize_and_list_tools()
                for restart in (False, True):
                    if restart:
                        client.send(control="restart")
                    client.expect(sql="CREATE TABLE ready (answer INTEGER)")
                    assert not list(workspace.glob("mcp-console-startup-*"))
                    # Observe the worker and its relay after startup handoff.
                    # Neither live environments nor the OS exec snapshot may
                    # retain the credential-bearing captured source.
                    # fmt: python
                    inspect = code("""
                        import os
                        import subprocess

                        marker = "captured-startup-credential-marker"
                        assert marker not in repr(dict(os.environ))
                        assert marker.encode() not in subprocess.check_output(["/usr/bin/env"])
                        for pid in (os.getpid(), os.getppid()):
                            environment = subprocess.check_output(["ps", "eww", "-p", str(pid)])
                            assert marker.encode() not in environment, "captured startup source remains in OS environment"
                        """)
                    client.expect(python=inspect)
                client.finish()
            records.append({"startup": language, "exec_environment_consumed": True})
    return records


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_preserves_aliased_connection(
    binary: Path, execution: Execution
) -> Transcript:
    return aliased_connection(binary, execution, with_r=True)


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_preserves_aliased_connection_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    return aliased_connection(binary, execution, with_r=False)


def aliased_connection(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    records = []
    for startup in (False, True):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            # fmt: python
            source = code("""
                import sqlite3


                class AliasedConnection:
                    def __init__(self) -> None:
                        self.native = sqlite3.connect(":memory:")
                        self.result = self.native.cursor()

                    def cursor(self) -> "AliasedConnection":
                        return self

                    def execute(self, source: str) -> "AliasedConnection":
                        self.result.execute(source)
                        return self

                    @property
                    def description(self) -> tuple | None:
                        return self.result.description

                    def fetchmany(self, size: int) -> list[tuple]:
                        return self.result.fetchmany(size)

                    def close(self) -> None:
                        self.native.close()


                native = AliasedConnection()
                _ = native.execute("CREATE TABLE selected (answer INTEGER)")
                _ = native.execute("INSERT INTO selected VALUES (42)")
                console_sql_connection(native)
                """)
            if startup:
                configure(workspace, "python", source)
            environment = os.environ if with_r else no_r_environment(workspace)
            with McpClient(
                installed_console(binary),
                execution.serve("-c", f"python={sys.executable}"),
                environment,
                workspace,
            ) as client:
                client.initialize_and_list_tools()
                if not startup:
                    client.expect(python=source)
                client.expect("answer\n------\n42\n", sql="SELECT answer FROM selected")
                client.expect(sql="UPDATE selected SET answer = 43")
                client.expect("answer\n------\n43\n", sql="SELECT answer FROM selected")
                client.expect(
                    # fmt: python
                    python=code("""
                        assert native.native.in_transaction
                        native.native.rollback()
                        assert native.native.execute("SELECT count(*) FROM selected").fetchone() == (0,)
                        """),
                )
                client.finish()
                records.append(
                    {"startup": startup, "aliased_connection_preserved": True}
                )
    return records


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_publishes_plots_before_sql(
    binary: Path, execution: Execution
) -> Transcript:
    return startup_plots(binary, execution, with_r=True)


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_publishes_plots_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    return startup_plots(binary, execution, with_r=False)


def startup_sql_result(client: McpClient) -> ToolResult:
    """Submit once and collect ordered startup output until SQL completes."""
    deadline = time.monotonic() + client.response_timeout
    first_call = len(client.transcript)
    result = client.send(sql="SELECT 42 AS answer", timeout_ms=0)
    content: list[dict] = []
    running = "\n[running; poll with an empty send]"
    while True:
        assert result.get("isError") is not True, result
        chunk = without_elapsed_result(result)["content"]
        last = chunk[-1]
        pending = last["type"] == "text" and last["text"].endswith(running)
        for item in chunk:
            if item is last and pending:
                item = {**item, "text": item["text"].removesuffix(running)}
                if not item["text"]:
                    continue
            if item == {"type": "text", "text": "[done]"} and content:
                continue
            if content and content[-1]["type"] == item["type"] == "text":
                content[-1]["text"] += item["text"]
            else:
                content.append(item.copy())
        if not pending:
            break
        remaining = deadline - time.monotonic()
        assert remaining > 0, "startup SQL did not complete"
        result = client.send(timeout_ms=max(1, int(remaining * 1_000)))
    result["content"] = content
    submitted = client.transcript[first_call]
    submitted["result"] = result
    client.transcript[first_call:] = [submitted]
    return result


def startup_plots(binary: Path, execution: Execution, *, with_r: bool) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        python, _ = isolated_python(root)
        subprocess.run(
            ["uv", "pip", "install", "--python", python, "matplotlib"],
            check=True,
            capture_output=True,
        )
        for failed in (False, True):
            workspace = root / ("failure" if failed else "success")
            workspace.mkdir()
            environment = (
                matplotlib_test_environment(workspace / "cache")
                if with_r
                else no_r_environment(workspace)
            )
            environment.update(
                TMPDIR=str(workspace), MPLBACKEND="Agg", MPL_IGNORE_SYSTEM_FONTS="1"
            )
            # fmt: python
            source = code("""
                import os
                import sqlite3
                from pathlib import Path

                import matplotlib.pyplot as plt

                startup_count = globals().get("startup_count", 0) + 1
                native = sqlite3.connect(":memory:")
                console_sql_connection(native)
                figure, axes = plt.subplots(figsize=(3, 2), dpi=100)
                _ = axes.plot([1, 2, 3], [3, 1, 2])
                figure.savefig(Path(os.environ["TMPDIR"]) / "startup.png", format="png")
                plt.show()
                """)
            if failed:
                source += "\nraise RuntimeError('startup failed after plotting')"
            config = configure(workspace, "python", source)
            settings = json.loads(config.read_text())
            settings["python"] = str(python)
            config.write_text(json.dumps(settings))
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                result = startup_sql_result(client)
                text = "".join(
                    item["text"] for item in result["content"] if item["type"] == "text"
                )
                if failed:
                    assert "RuntimeError: startup failed after plotting" in text, text
                    assert "SQL unavailable" in text, text
                else:
                    assert text == "answer\n------\n42\n", text
                reference = wait_for_worker_file(workspace, "startup.png", client)
                assert (
                    sum(item["type"] == "image" for item in result["content"]) == 1
                ), (failed, text)
                assert_result_content(
                    client,
                    [
                        reference.read_bytes()
                        if item["type"] == "image"
                        else item["text"]
                        for item in result["content"]
                    ],
                    image_reference="live startup savefig {page}",
                )
                client.expect("\n[idle]")
                client.expect(
                    python="assert startup_count == 1; assert not plt.get_fignums()"
                )
                if failed:
                    client.send(sql="SELECT 42 AS answer")
                    assert "SQL unavailable" in last_tool_text(client)
                    assert all(
                        item["type"] == "text"
                        for item in client.transcript[-1]["result"]["content"]
                    )
                client.finish()
                records.append({"failed_startup": failed})
                records.extend(client.transcript[3:])
    return records


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_preserves_identity_transactions_and_captured_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: python
        source = code("""
            import os
            import sqlite3
            import subprocess


            def assert_startup_transport_consumed() -> None:
                assert "MCP_CONSOLE_STARTUP_FILE" not in os.environ
                assert b"MCP_CONSOLE_STARTUP_FILE=" not in subprocess.check_output(["/usr/bin/env"])


            assert_startup_transport_consumed()

            startup_count = globals().get("startup_count", 0) + 1
            native = sqlite3.connect(":memory:")
            _ = native.execute("CREATE TABLE selected (answer INTEGER)")
            _ = native.execute("INSERT INTO selected VALUES (42)")
            console_sql_connection(native)
            """)
        # Exercise the documented limit with UTF-8 and JSON-escaped characters,
        # including disabled environment inheritance and captured restart.
        source = "#" + 'é雪"\\' * 1000 + "\n" + source
        payload = json.dumps(
            {"language": "python", "code": source},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        encoded_bytes = len(payload.encode("utf-8"))
        source = "#" + "a" * (32 * 1024 - encoded_bytes - 3) + "\n" + source
        payload = json.dumps(
            {"language": "python", "code": source},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        assert len(payload.encode("utf-8")) == 32 * 1024
        config = configure(workspace, "r", source)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "startup.language=python",
                "-c",
                "sandbox.inherit_environment=false",
            ),
            os.environ,
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            schema = json.dumps(client.transcript[-1])
            assert "startup_count" not in schema and "sqlite3.connect" not in schema
            client.expect("answer\n------\n42\n", sql="SELECT answer FROM selected")
            client.expect(
                # fmt: python
                python=code("""
                    assert_startup_transport_consumed()
                    assert startup_count == 1
                    assert native.in_transaction
                    native_identity = id(native)
                    """),
            )
            client.expect(sql="UPDATE selected SET answer = 43")
            client.expect(
                # fmt: python
                python=code("""
                    assert startup_count == 1 and id(native) == native_identity
                    assert native.execute("SELECT answer FROM selected").fetchone() == (43,)
                    native.rollback()
                    assert native.execute("SELECT count(*) FROM selected").fetchone() == (0,)
                    """),
            )
            client.expect(python="console_sql_connection(None)")
            client.expect(sql="CREATE TABLE managed_retained AS SELECT 7 AS answer")
            client.expect(
                python="console_sql_connection(None); assert startup_count == 1 and id(native) == native_identity"
            )
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1       7\n",
                sql="SELECT answer FROM managed_retained",
            )
            client.expect(
                python="assert native.execute('SELECT count(*) FROM selected').fetchone() == (0,)"
            )
            # Restart uses launch-captured source, not the changed configuration file.
            config.write_text(
                json.dumps(
                    {
                        "startup": {
                            "language": "python",
                            "code": "raise RuntimeError('changed')",
                        }
                    }
                )
            )
            client.send(control="restart")
            client.expect("answer\n------\n42\n", sql="SELECT answer FROM selected")
            client.expect(
                python="assert_startup_transport_consumed(); assert startup_count == 1 and native.in_transaction"
            )
            client.finish()
    return [
        {
            "startup": "native Python with R present",
            "once_per_generation": True,
            "identity_and_transactions": True,
            "restart_uses_captured_source": True,
        }
    ]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_python_startup_without_r(binary: Path, execution: Execution) -> Transcript:
    from boundaries.client_server.sql.test_without_r import environment, sql_client

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: python
        source = code("""
            import os
            import sqlite3
            import subprocess


            def assert_startup_transport_consumed() -> None:
                assert "MCP_CONSOLE_STARTUP_FILE" not in os.environ
                assert b"MCP_CONSOLE_STARTUP_FILE=" not in subprocess.check_output(["/usr/bin/env"])


            assert_startup_transport_consumed()

            native = sqlite3.connect(":memory:")
            _ = native.execute("CREATE TABLE selected AS SELECT 42 AS answer")
            console_sql_connection(native)
            """)
        config = configure(workspace, "python", source)
        settings = json.loads(config.read_text())
        settings["python"] = sys.executable
        config.write_text(json.dumps(settings))
        with sql_client(binary, execution, environment(workspace), workspace) as client:
            client.expect("answer\n------\n42\n", sql="SELECT answer FROM selected")
            client.expect(
                python="assert_startup_transport_consumed(); assert sql_connection() is native; assert native.execute('SELECT answer FROM selected').fetchone() == (42,)"
            )
            client.finish()
    return [{"startup": "native Python without R", "selected_connection": True}]


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_r_startup_preserves_native_identity_and_transaction(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: r
        source = code("""
            assert_startup_transport_consumed <- function() {
              stopifnot(is.na(Sys.getenv(
                "MCP_CONSOLE_STARTUP_FILE",
                unset = NA_character_
              )))
              stopifnot(
                !any(startsWith(
                  system2("/usr/bin/env", stdout = TRUE),
                  "MCP_CONSOLE_STARTUP_FILE="
                ))
              )
            }
            assert_startup_transport_consumed()
            startup_count <- if (exists("startup_count")) startup_count + 1L else 1L
            native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
            invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
            DBI::dbBegin(native)
            invisible(DBI::dbExecute(native, "UPDATE selected SET answer = 42"))
            console_sql_connection(native)
            """)
        configure(workspace, "r", source)
        with McpClient(binary, execution.serve(), os.environ, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
                sql="SELECT answer FROM selected",
            )
            client.expect(
                # fmt: r
                r=code("""
                    assert_startup_transport_consumed()
                    stopifnot(startup_count == 1L, identical(sql_connection(), native))
                    DBI::dbRollback(native)
                    stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                    """),
            )
            client.send(control="restart")
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
                sql="SELECT answer FROM selected",
            )
            client.expect(
                r="assert_startup_transport_consumed(); stopifnot(startup_count == 1L, identical(sql_connection(), native))"
            )
            client.finish()
    return [
        {
            "startup": "native R",
            "once_per_generation": True,
            "identity_and_transactions": True,
        }
    ]


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_failed_startup_withholds_sql_and_preserves_partial_effects(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for language in ("python", "r"):
        sources = (
            [
                (
                    "startup_count = 1\nraise RuntimeError('startup failure')",
                    "startup failure",
                ),
                ("private_credential = 'sentinel-secret'; invalid !!!", "SyntaxError"),
                ("console_sql_connection(object())", "cursor()"),
                (
                    "import sqlite3\nnative = sqlite3.connect(':memory:')\nnative.close()\nconsole_sql_connection(native)",
                    "closed database",
                ),
                ("startup_count = 1", "startup must select"),
                (
                    "import sqlite3\nnative = sqlite3.connect(':memory:')\nconsole_sql_connection(native)\nstartup_count = 1\nraise RuntimeError('after selection')",
                    "after selection",
                ),
            ]
            if language == "python"
            else [
                ('startup_count <- 1L; stop("startup failure")', "startup failure"),
                (
                    'private_credential <- "sentinel-secret"; invalid !!!',
                    "R startup syntax error",
                ),
                ("console_sql_connection(new.env())", "valid DBIConnection"),
                (
                    "native <- DBI::dbConnect(duckdb::duckdb()); DBI::dbDisconnect(native); console_sql_connection(native)",
                    "valid DBIConnection",
                ),
                ("startup_count <- 1L", "startup must select"),
                (
                    'native <- DBI::dbConnect(duckdb::duckdb()); console_sql_connection(native); startup_count <- 1L; stop("after selection")',
                    "after selection",
                ),
            ]
        )
        for source, diagnostic in sources:
            with tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary)
                configure(workspace, language, source)
                with McpClient(
                    binary, execution.serve(), os.environ, workspace
                ) as client:
                    client.initialize_and_list_tools()
                    client.send(sql="CREATE TABLE never_run AS SELECT 99 AS answer")
                    first = last_tool_text(client)
                    assert diagnostic in first and "SQL unavailable" in first, first
                    assert "Unexpected vector type" not in first, first
                    assert "sentinel-secret" not in first, first
                    client.send(sql="SELECT 42 AS answer")
                    second = last_tool_text(client)
                    assert "SQL unavailable" in second and "42" not in second, second
                    if "startup_count" in source:
                        client.expect(
                            **{
                                language: "assert startup_count == 1"
                                if language == "python"
                                else "stopifnot(startup_count == 1L)"
                            }
                        )
                    if diagnostic == "after selection":
                        # The native interpreter remains usable. Explicit reset selects
                        # the managed default but cannot erase a failed startup receipt.
                        client.expect(
                            **{
                                language: "console_sql_connection(None)"
                                if language == "python"
                                else "console_sql_connection(NULL)"
                            }
                        )
                        client.send(sql="SELECT 42 AS answer")
                        assert "SQL unavailable" in last_tool_text(client)
                    client.finish()
                    records.append(
                        {
                            "language": language,
                            "failure": diagnostic,
                            "first_sql": first,
                            "later_sql": second,
                        }
                    )
    return records


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_missing_r_startup_withholds_sql(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.sql.test_without_r import environment, sql_client

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = configure(workspace, "r", 'stop("must not execute")')
        settings = json.loads(config.read_text())
        settings["python"] = sys.executable
        config.write_text(json.dumps(settings))
        with sql_client(binary, execution, environment(workspace), workspace) as client:
            client.send(sql="SELECT 42 AS answer")
            output = last_tool_text(client)
            assert "R is unavailable" in output and "SQL unavailable" in output, output
            client.expect(python="assert 6 * 7 == 42")
            client.send(sql="SELECT 42 AS answer")
            assert "SQL unavailable" in last_tool_text(client)
            client.finish()
    return [{"missing_runtime_withholds_sql": output}]


@requires(POSIX, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_missing_selected_python_has_no_sql_fallback(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = configure(
            workspace, "python", "raise RuntimeError('source must not execute')"
        )
        settings = json.loads(config.read_text())
        settings["python"] = "/mcp-console-startup-missing-python"
        config.write_text(json.dumps(settings))
        with McpClient(binary, execution.serve(), os.environ, workspace) as client:
            client.initialize_and_list_tools()
            client.send(sql="SELECT 42 AS answer")
            output = client.transcript[-1]["result"]["content"][0]["text"]
            assert "mcp-console-startup-missing-python" in output, output
            assert "source must not execute" not in output and "42" not in output, (
                output
            )
            client.finish()
    return [{"missing_selected_python": output}]


@requires(POSIX, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_startup_gate_preserves_discovery_ordering_and_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for interrupt in (False, True):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            # fmt: python
            source = code("""
                import sqlite3

                startup_count = globals().get("startup_count", 0) + 1
                native = sqlite3.connect(":memory:")
                console_sql_connection(native)
                input("Startup gate> ")
                _ = native.execute("CREATE TABLE selected AS SELECT 42 AS answer")
                """)
            configure(workspace, "python", source)
            with McpClient(binary, execution.serve(), os.environ, workspace) as client:
                # Discovery and ping complete while source cannot pass its input gate.
                client.initialize_and_list_tools()
                client.request("ping")
                client.expect(
                    '[input requested: "Startup gate> "]\n[waiting for stdin]',
                    sql="CREATE TABLE first_cell AS SELECT answer FROM selected",
                )
                if interrupt:
                    client.send(control="interrupt", timeout_ms=0)
                    interrupted = last_tool_text(client)
                    assert "KeyboardInterrupt" in interrupted, interrupted
                    client.expect(
                        # fmt: python
                        python=code("""
                            assert startup_count == 1
                            assert native.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)
                            """),
                    )
                    client.send(sql="SELECT 42 AS answer")
                    assert "SQL unavailable" in last_tool_text(client)
                    # Explicit restart authorizes one new attempt; same-call input
                    # and SQL belong to the replacement generation.
                    client.send(
                        control="restart",
                        stdin="continue\n",
                        sql="SELECT answer FROM selected",
                    )
                    assert "42" in last_tool_text(client), client.transcript[-1]
                    client.expect(python="assert startup_count == 1")
                else:
                    client.expect(stdin="continue\n", timeout_ms=0)
                    client.expect(
                        "answer\n------\n42\n", sql="SELECT answer FROM first_cell"
                    )
                    client.expect(python="assert startup_count == 1")
                records.append(
                    {
                        "interrupted": interrupt,
                        "calls": normalize_request_ids(client.finish()[3:]),
                    }
                )
    return records


@requires(POSIX, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_crashed_startup_is_not_automatically_replayed(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: python
        source = code("""
            import os
            import sqlite3
            from pathlib import Path

            attempts = Path("attempts")
            previous = attempts.read_text() if attempts.exists() else ""
            _ = attempts.write_text(previous + "x")
            if not previous:
                os._exit(17)
            native = sqlite3.connect(":memory:")
            console_sql_connection(native)
            """)
        configure(workspace, "python", source)
        arguments = execution.serve(
            *(("--writable-root", str(workspace)) if execution is SANDBOXED else ())
        )
        with McpClient(binary, arguments, os.environ, workspace) as client:
            client.initialize_and_list_tools()
            client.send(sql="SELECT 42 AS answer")
            first = client.transcript[-1]["result"]["content"][0]["text"]
            assert "worker stopped" in first or "worker failed" in first, first
            client.send(sql="SELECT 42 AS answer")
            refusal = client.transcript[-1]["result"]["content"][0]["text"]
            assert "explicit restart required" in refusal, refusal
            assert (workspace / "attempts").read_text() == "x"
            client.send(control="restart", sql="SELECT 42 AS answer")
            assert "42" in last_tool_text(client), client.transcript[-1]
            assert (workspace / "attempts").read_text() == "xx"
            client.finish()
    return [
        {
            "crashed_startup": first,
            "automatic_replay_refused": refusal,
            "explicit_restart_attempts": 2,
        }
    ]


@requires(POSIX, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_incomplete_python_setup_never_retries_startup_source(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.sql.test_configuration import python_setup_checkpoint

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, "python", "startup_count = 1")
        (workspace / "sitecustomize.py").write_text(
            f"exec(compile({json.dumps(python_setup_checkpoint())}, '<SQL setup checkpoint>', 'exec'))"
        )
        environment = dict(os.environ, RETICULATE_PYTHONPATH=str(workspace))
        with McpClient(binary, execution.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                '[input requested: "Python SQL setup> "]\n[waiting for stdin]',
                sql="CREATE TABLE never_run AS SELECT 99 AS answer",
            )
            client.send(control="interrupt", timeout_ms=0)
            assert "KeyboardInterrupt" in last_tool_text(client)
            client.expect(
                "Python setup resumed with same objects\n",
                python='assert "startup_count" not in globals()',
            )
            client.send(sql="SELECT 42 AS answer")
            assert "SQL unavailable" in last_tool_text(client)
            return client.finish()[3:]


@requires(POSIX, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_r_startup_interrupt_preserves_transaction_without_replay(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: r
        source = code("""
            startup_count <- 1L
            native <- DBI::dbConnect(duckdb::duckdb())
            invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
            DBI::dbBegin(native)
            console_sql_connection(native)
            invisible(readline("R startup gate> "))
            invisible(DBI::dbExecute(native, "UPDATE selected SET answer = 42"))
            """)
        configure(workspace, "r", source)
        with McpClient(binary, execution.serve(), os.environ, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                '[input requested: "R startup gate> "]\n[waiting for stdin]',
                sql="UPDATE selected SET answer = 99",
            )
            client.send(control="interrupt", timeout_ms=0)
            assert "R startup interrupted" in last_tool_text(client)
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(startup_count == 1L, identical(sql_connection(), native))
                    stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                    DBI::dbRollback(native)
                    """),
            )
            client.send(sql="SELECT 42 AS answer")
            assert "SQL unavailable" in last_tool_text(client)
            return client.finish()[3:]


@requires(PROCESS_EVENTS, R, SQL)
@executions(DIRECT, SANDBOXED)
def test_closure_retires_captured_startup_resources(
    binary: Path, execution: Execution
) -> Transcript:
    from support.processes import (
        capture_process_identity,
        child_process_identities,
        kill_processes,
        live_processes,
    )

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        configure(workspace, "python", 'input("Startup close gate> ")')
        with McpClient(binary, execution.serve(), os.environ, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                '[input requested: "Startup close gate> "]\n[waiting for stdin]',
                sql="SELECT 42 AS answer",
            )
            descendants = []
            pending = [capture_process_identity(client.process.pid)]
            while pending:
                children = child_process_identities(pending.pop())
                descendants.extend(children)
                pending.extend(children)
            assert descendants
            try:
                client.close()
                assert not live_processes(descendants), (
                    "startup resources survived closure"
                )
                assert client.process.returncode == 0
            finally:
                kill_processes(descendants)
    return [{"blocked_captured_startup_retired": True}]
