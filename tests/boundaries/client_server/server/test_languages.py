"""Captured model-facing languages do not configure SQL's embedded providers."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires


def _configure(root: Path, languages: list[str]) -> Path:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"languages": languages}), encoding="utf-8")
    return config


def _environment(root: Path, *, without_r: bool = False) -> dict[str, str]:
    env = dict(os.environ, MCP_CONSOLE_HOME=str(root / "console-home"))
    env.pop("MCP_CONSOLE_LANGUAGES", None)
    if without_r:
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "uv").symlink_to(shutil.which("uv"))
        retain_system_bwrap(bin_dir)
        env["PATH"] = str(bin_dir)
        for name in (
            "R_HOME",
            "R_LIBS",
            "R_LIBS_USER",
            "RETICULATE_PYTHON",
            "RETICULATE_UV",
        ):
            env.pop(name, None)
    return env


def _tool(client: McpClient) -> dict:
    listed = client.request("tools/list")
    (tool,) = listed["result"]["tools"]
    return tool


@requires(SQL)
def test_configures_captured_tool_surface(binary: Path) -> Transcript:
    transcript = []
    for languages in (
        ["sql"],
        ["sql", "python"],
        ["r"],
        ["python"],
        ["r", "sql"],
        ["r", "python"],
        ["r", "python", "sql"],
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _configure(root, ["r", "python", "sql"])
            with McpClient(
                binary,
                DIRECT.serve(
                    "--worker",
                    "unused-worker",
                    "-c",
                    "languages=" + json.dumps(languages),
                ),
                _environment(root),
                current_directory=root,
            ) as client:
                client.initialize_and_list_tools()
                tool = _tool(client)
                fields = set(tool["inputSchema"]["properties"])
                assert fields == {
                    *languages,
                    "control",
                    "requirements",
                    "stdin",
                    "timeout_ms",
                }, fields
                description = tool["description"]
                cell_guidance = description.split("Send one complete ")[1].split(
                    " cell per call"
                )[0]
                assert {
                    name
                    for name in ("r", "python", "sql")
                    if f"`{name}`" in cell_guidance
                } == set(languages)
                config.write_text("languages: [r]\n", encoding="utf-8")
                assert _tool(client) == tool
                transcript.append({"languages": languages})
                transcript.extend(client.finish()[:3])
    return transcript


@requires(SQL)
def test_builtin_guidance_matches_visible_languages(binary: Path) -> Transcript:
    transcript = []
    for languages in (["sql"], ["sql", "python"], ["r", "sql"]):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _configure(root, languages)
            with McpClient(binary, DIRECT.serve(), _environment(root), root) as client:
                client.initialize_and_list_tools()
                tool = _tool(client)
                assert (
                    "Persistent "
                    + " and ".join(
                        name
                        for name in ("R", "Python", "SQL")
                        if name.lower() in languages
                    )
                    + " workbench"
                    in tool["description"]
                )
                assert "without a setup cell" in tool["description"]
                if "r" not in languages:
                    assert "`r`" not in tool["description"]
                properties = tool["inputSchema"]["properties"]
                sql_guidance = properties["sql"]["description"]
                assert "With R-owned managed DuckDB" in sql_guidance
                assert "Without R, managed DuckDB uses Python" in sql_guidance
                if "r" not in languages:
                    assert "from an R cell" not in sql_guidance
                if "python" in languages:
                    python_guidance = properties["python"]["description"]
                    assert "With R-owned DuckDB" in python_guidance
                    assert "`r.name`" in python_guidance
                    assert "Without R, `sql_connection()`" in python_guidance
                    assert "R plot rules" not in properties["python"]["description"]
                else:
                    assert (
                        "register(name, frame)" not in properties["sql"]["description"]
                    )
                assert {"r", "python", "duckdb"} <= set(
                    properties["requirements"]["properties"]
                )
                transcript.append({"languages": languages})
                transcript.extend(client.finish()[:3])
    return transcript


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_provider_guidance_is_independent_of_visibility(
    binary: Path, execution: Execution
) -> Transcript:
    transcript = []
    for languages in (["sql", "python"], ["r", "sql"]):
        advertised = None
        for without_r in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _configure(root, languages)
                with McpClient(
                    installed_console(binary),
                    execution.serve(),
                    _environment(root, without_r=without_r),
                    root,
                ) as client:
                    client.initialize_and_list_tools()
                    tool = _tool(client)
                    description = tool["description"]
                    assert "R when available and Python otherwise" in description
                    if advertised is None:
                        advertised = tool
                    else:
                        assert tool == advertised
                    if "python" in languages:
                        assert "R-owned managed DuckDB" in description
                        assert "Without R, Python-owned DuckDB" in description
                        client.send(
                            # fmt: python
                            python=code("""
                                import pandas as pd

                                frame = pd.DataFrame({"value": [19, 23]})
                                """)
                        )
                        assert "Error" not in last_tool_text(client)
                        if without_r:
                            client.send(
                                python='_ = sql_connection().register("visible_frame", frame)'
                            )
                        else:
                            client.send(python="r.visible_frame = frame")
                        assert "Error" not in last_tool_text(client)
                        client.send(
                            sql="SELECT sum(value) AS answer FROM visible_frame"
                        )
                    else:
                        client.send(sql="SELECT 42 AS answer")
                    assert "42" in last_tool_text(client), last_tool_text(client)
                    assert _tool(client) == tool
                    transcript.append(
                        {
                            "languages": languages,
                            "provider": "Python" if without_r else "R",
                        }
                    )
                    session = client.finish()
                    transcript.extend(session[:3])
                    transcript.extend(entry for entry in session[3:] if "send" in entry)
    return transcript


@requires(SQL, POSIX)
def test_rejects_hidden_source_before_custom_worker_effects(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _configure(root, ["sql"])
        started = root / "worker-started"
        env = dict(_environment(root), MCP_CONSOLE_TEST_ZOD_STARTED=str(started))
        with McpClient(binary, DIRECT.serve("--worker", str(zod)), env, root) as client:
            client.initialize_and_list_tools()
            for language in ("r", "python"):
                for source in (None, "must not run"):
                    result = client.send(
                        **{language: source},
                        control="restart",
                        requirements={"r": ["must-not-prepare"]},
                        stdin="must not queue\n",
                    )
                    assert result.get("isError"), result
                    assert result["content"] == [
                        {
                            "type": "text",
                            "text": f"`{language}` source fields are hidden by `languages`",
                        }
                    ], result
            assert not started.exists(), "rejected call launched a worker"
            result = client.send(sql="SELECT 1", wait_ms=0)
            assert "`r`" not in result["content"][0]["text"]
            assert "`python`" not in result["content"][0]["text"]
            transcript = client.finish()
            return transcript[:3] + [
                entry for entry in transcript[3:] if "send" in entry
            ]


@requires(SQL)
def test_invalid_language_configuration_fails_launch(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for value in ([], ["SQL"], ["ruby"], "sql", [1]):
            result = subprocess.run(
                [
                    str(binary),
                    *DIRECT.serve(
                        "--worker",
                        "unused-worker",
                        "-c",
                        "languages=" + json.dumps(value),
                    ),
                ],
                cwd=root,
                env=_environment(root),
                input="",
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode != 0, result
            assert "languages" in result.stderr, result.stderr
            transcript.append({"languages": value, "stderr": result.stderr})
    return transcript


def _catalog(client: McpClient) -> None:
    for source in (
        "CREATE TABLE retained(value INTEGER)",
        "INSERT INTO retained VALUES (41)",
        "BEGIN",
        "INSERT INTO retained VALUES (99)",
        "ROLLBACK",
        "BEGIN",
        "INSERT INTO retained VALUES (1)",
        "COMMIT",
    ):
        client.send(sql=source)
        assert "Error" not in last_tool_text(client), last_tool_text(client)
    client.send(sql="SELECT sum(value) AS answer FROM retained")
    assert "42" in last_tool_text(client), last_tool_text(client)


def _sql_profile(
    binary: Path,
    execution: Execution,
    *,
    without_r: bool,
    disabled_bootstrap: bool = False,
    python_visible: bool = False,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = _configure(root, ["sql", "python"] if python_visible else ["sql"])
        env = _environment(root, without_r=without_r)
        if disabled_bootstrap:
            env["MCP_CONSOLE_LANGUAGES"] = "sql"
        with McpClient(
            installed_console(binary), execution.serve(), env, root
        ) as client:
            client.initialize_and_list_tools()
            tool = _tool(client)
            properties = tool["inputSchema"]["properties"]
            assert set(properties) & {"r", "python", "sql"} == (
                {"sql", "python"} if python_visible else {"sql"}
            )
            assert "DuckDB SQL first" in tool["description"]
            assert "`r`" not in tool["description"]
            assert "from an R cell" not in properties["sql"]["description"]
            if not python_visible:
                assert "register(name, frame)" not in properties["sql"]["description"]
            _catalog(client)
            assert _tool(client) == tool
            if python_visible:
                client.send(python="print(6 * 7)")
                assert last_tool_text(client) == "42\n"
                client.send(python="1", sql="SELECT 1")
                assert (
                    client.transcript[-1]["result"]["content"][0]["text"]
                    == "only one of `python` or `sql` may be supplied"
                )
            for language in ("r",) if python_visible else ("r", "python"):
                result = client.send(
                    **{language: "must not run"},
                    control="restart",
                    requirements={"action": "set"},
                )
                assert result.get("isError"), result
            client.send(sql="SELECT sum(value) AS answer FROM retained")
            assert "42" in last_tool_text(client)
            inspected = client.send(requirements={"action": "get"})
            assert {"r", "python", "duckdb"} <= set(
                inspected["structuredContent"]["requirements"]
            )
            client.send()
            client.send(control="interrupt")
            config.write_text("languages: [r]\n", encoding="utf-8")
            client.send(control="restart")
            client.send(sql="SHOW TABLES")
            assert "Error" not in last_tool_text(client)
            assert "retained" not in last_tool_text(client)
            assert _tool(client) == tool
            # The provider-specific default inventory belongs to requirements tests.
            # Assert inspection above; retain the handshake and SQL/control behavior.
            transcript = client.finish()
            return transcript[:3] + [
                entry
                for entry in transcript[3:]
                if "send" in entry
                and entry["send"] != {"requirements": {"action": "get"}}
            ]


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_hidden_r_managed_sql(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=False)


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_python_with_hidden_r(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=False, python_visible=True)


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_sql_with_disabled_direct_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    return _sql_profile(binary, execution, without_r=False, disabled_bootstrap=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_hidden_python_managed_sql(binary: Path, execution: Execution) -> Transcript:
    return _sql_profile(binary, execution, without_r=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_with_disabled_python_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    return _sql_profile(binary, execution, without_r=True, disabled_bootstrap=True)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_missing_provider_recovers_without_setup_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        _configure(root, ["sql"])
        with McpClient(
            installed_console(binary),
            execution.serve(),
            _environment(root, without_r=True),
            root,
        ) as client:
            client.initialize_and_list_tools()
            tool = _tool(client)
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_tool_text(client)
            client.send(control="restart", requirements={"action": "set"})
            client.send(sql="SELECT 1")
            diagnostic = last_tool_text(client)
            assert "DuckDB is unavailable" in diagnostic
            assert "requirements.python" in diagnostic and "restart" in diagnostic
            assert _tool(client) == tool
            client.send(control="restart", requirements={"python": ["duckdb"]})
            client.send(sql="SELECT 42 AS recovered")
            assert "42" in last_tool_text(client)
            assert _tool(client) == tool
            transcript = client.finish()
            return transcript[:3] + [
                entry for entry in transcript[3:] if "send" in entry
            ]
