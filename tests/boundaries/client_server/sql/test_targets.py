"""Configured managed SQL on capability-supported execution targets."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.docker import (
    DOCKER,
    configure as configure_docker,
    image,
    workspace as docker_workspace,
)
from support.docker_sandbox import (
    DOCKER_SANDBOX,
    configure as configure_sbx,
    workspace as sbx_workspace,
)
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import SQL, requires
from support.ssh import client_environment
from support.ssh_external import EXTERNAL_SSH, external_target
from support.suites import run_this_suite


def exercise_catalog(
    client: McpClient, provider: str, *, prepare: bool = False
) -> None:
    wait_for_evaluation_output(
        client,
        None,
        "execution-target managed catalog creation",
        completion_timeout_seconds=client.response_timeout,
        **(
            {"requirements": {"python": ["duckdb"]}}
            if prepare and provider == "python"
            else {}
        ),
        sql="CREATE TABLE durable AS SELECT 42 AS answer",
    )
    assert "Error" not in last_tool_text(client), client.transcript[-1]
    client.send(control="restart")
    assert "Error" not in last_tool_text(client), client.transcript[-1]
    client.send(sql="SELECT answer, current_setting('threads') AS threads FROM durable")
    output = last_tool_text(client)
    assert "42" in output and "2" in output and "Error" not in output, (
        client.transcript[-1]
    )
    if provider == "r":
        client.expect(r="stopifnot(inherits(sql_connection(), 'duckdb_connection'))")
    else:
        client.expect(
            python="import duckdb; assert isinstance(sql_connection(), duckdb.DuckDBPyConnection)"
        )


@requires(SQL, EXTERNAL_SSH)
@executions(DIRECT, SANDBOXED)
def test_external_prepares_configured_python_extensions(
    binary: Path, execution: Execution
) -> Transcript:
    with external_target() as external, tempfile.TemporaryDirectory() as directory:
        local = Path(directory)
        controller_cache = local / "controller-extensions"
        config = local / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "target": external["target"],
                    "sandbox": {"environment": external["environment"]},
                    "sql": {"provider": "python"},
                }
            )
        )
        environment = client_environment(
            local, config=external.get("ssh_config"), remote_path=external.get("path")
        )
        environment["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(controller_cache)
        with McpClient(binary, execution.serve(), environment, local) as client:
            client.initialize_and_list_tools()
            prepared = client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.4.4"],
                    "duckdb": ["fts"],
                }
            )
            assert not prepared.get("isError"), prepared
            client.expect(
                r='stopifnot(as.character(packageVersion("duckdb")) != "1.4.4")'
            )
            client.expect(
                "Success\n-------\n[0 rows]\n",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            client.expect(
                # fmt: python
                python=code("""
                    import duckdb

                    assert duckdb.__version__ == "1.4.4"
                    directory = (
                        sql_connection()
                        .execute("SELECT current_setting('extension_directory')")
                        .fetchone()[0]
                    )
                    assert directory != "<controller-cache>"
                    """).replace("'<controller-cache>'", repr(str(controller_cache)))
            )
            client.send(control="restart")
            client.expect(
                "Success\n-------\n[0 rows]\n",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            client.finish()
        assert not controller_cache.exists()
    return [
        {
            "configured_python_extensions_prepared_on_execution_host": True,
            "restart": True,
        }
    ]


@requires(SQL, EXTERNAL_SSH)
@executions(DIRECT, SANDBOXED)
def test_external_sql_settings_resolve_on_execution_host(
    binary: Path, execution: Execution
) -> Transcript:
    with external_target() as external, tempfile.TemporaryDirectory() as directory:
        local = Path(directory)
        config = local / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        environment = client_environment(
            local, config=external.get("ssh_config"), remote_path=external.get("path")
        )
        for provider in ("r", "python"):
            database = f"cli/configured-{execution.name}-{provider}.duckdb"
            config.write_text(
                json.dumps(
                    {
                        "target": external["target"],
                        "sandbox": {"environment": external["environment"]},
                        "sql": {
                            "provider": provider,
                            "database": database,
                            "options": {"threads": 2},
                        },
                    }
                )
            )
            arguments = (
                execution.serve("--writable-root", "cli")
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, arguments, environment, local) as client:
                client.initialize_and_list_tools()
                exercise_catalog(client, provider, prepare=True)
                if provider == "r":
                    client.expect(r=f"stopifnot(file.exists('{database}'))")
                else:
                    client.expect(
                        python=f"from pathlib import Path; assert Path('{database}').is_file()"
                    )
                client.finish()
            assert not (local / database).exists()
    return [
        {
            "execution_host_relative_database": True,
            "captured_restart_settings": True,
            "providers": ["r", "python"],
        }
    ]


def exercise_prepared_target(binary: Path, root: Path) -> Transcript:
    config = root / ".agents/console/config.yaml"
    captured = json.loads(config.read_text())
    for provider in ("r", "python"):
        # Bind persistence through the existing target mount contract.
        config.write_text(
            json.dumps(
                {
                    **captured,
                    "sql": {
                        "provider": provider,
                        "database": f"configured-{provider}.duckdb",
                        "options": {"threads": 2},
                    },
                }
            )
        )
        with McpClient(binary, ("serve",), current_directory=root) as client:
            client.initialize_and_list_tools()
            exercise_catalog(client, provider)
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["python"] == [], (
                inspected
            )
            client.finish()
        assert (root / f"configured-{provider}.duckdb").is_file()
    return [
        {
            "prepared_preinstalled_sql_settings": True,
            "captured_restart_settings": True,
            "providers": ["r", "python"],
        }
    ]


@requires(DOCKER)
def test_docker_sql_settings(binary: Path) -> Transcript:
    with docker_workspace() as root:
        configure_docker(
            root,
            image(),
            mounts=[
                {"source": str(root), "target": "/workspace", "access": "read_write"}
            ],
        )
        return exercise_prepared_target(binary, root)


@requires(DOCKER_SANDBOX)
def test_sbx_sql_settings(binary: Path) -> Transcript:
    with sbx_workspace() as root:
        configure_sbx(
            root,
            mounts=[
                {"source": str(root), "target": "/workspace", "access": "read_write"}
            ],
        )
        return exercise_prepared_target(binary, root)


if __name__ == "__main__":
    run_this_suite(__file__)
