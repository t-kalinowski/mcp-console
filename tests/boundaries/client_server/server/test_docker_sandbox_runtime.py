#!/usr/bin/env -S uv run --script
"""Actual microVM acceptance. Fake peers cannot satisfy this capability."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.docker_sandbox import (
    DOCKER_SANDBOX,
    absent,
    calls,
    cli_peer,
    configure,
    isolated_controller,
    finish,
    generations,
    sbx,
    workspace,
)
from support.normalization import code
from support.requirements import requires
from support.suites import run_this_suite


@requires(DOCKER_SANDBOX)
def test_mixed_runtime_shares_recordings_and_restart_without_native(
    binary: Path,
) -> list:
    with workspace(real=True) as root:
        project = root / "project"
        project.mkdir()
        readonly = root / 'read only, "quoted"; $(never-execute)'
        readonly.mkdir()
        (readonly / "value").write_text("read-only data")
        relocated, environment = isolated_controller(binary, root, real=True)
        environment.update(
            R_HOME="/controller-r-must-not-be-used",
            RETICULATE_PYTHON="/controller-python-must-not-be-used",
        )
        config = configure(
            project,
            workspace=str(project),
            mounts=[
                {"source": ".", "target": str(project), "access": "read_write"},
                {
                    "source": "../" + readonly.name,
                    "target": str(readonly),
                    "access": "read_only",
                },
            ],
            environment={
                "CONSOLE_LITERAL": 'spaces "quotes" $HOME',
                "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "1",
            },
        )
        with McpClient(relocated, ("serve",), environment, project) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            assert "requirements" not in tool["inputSchema"]["properties"]
            assert (
                "microVM" in tool["description"]
                and "without a sandbox" not in tool["description"]
            )
            client.send(
                r=f"x <- 41; stopifnot(getwd() == {json.dumps(str(project))}); x + 1"
            )
            assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            client.send(
                # fmt: python
                python=code(f"""
                    import os, sys, shutil
                    from pathlib import Path
                    assert sys.platform == "linux"
                    assert os.environ["CONSOLE_LITERAL"] == 'spaces "quotes" $HOME'
                    assert os.environ["MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION"] == "0"
                    assert os.environ["RETICULATE_USE_MANAGED_VENV"] == "no"
                    assert shutil.which("uv")
                    assert not Path("/usr/local/libexec/mcp-console-sandbox").exists()
                    assert not Path("/usr/local/bin/mcp-console-sandbox").exists()
                    sentinels = Path("/tmp/resolver-sentinels")
                    sentinels.mkdir()
                    for name in ("uv", "ir", "mcp-console-sandbox"):
                        executable = sentinels / name
                        executable.write_text("#!/bin/sh\\necho invoked >> /tmp/resolver-invoked\\nexit 99\\n")
                        executable.chmod(0o755)
                    os.environ["PATH"] = str(sentinels) + os.pathsep + os.environ["PATH"]
                    assert Path({str(readonly / "value")!r}).read_text() == "read-only data"
                    try:
                        Path({str(readonly / "forbidden")!r}).write_text("bad")
                    except OSError:
                        print("read-only share enforced")
                    else:
                        raise AssertionError("read-only share was writable")
                    Path("result.txt").write_text("persistent share")
                    Path("/tmp/generation-state").write_text("ephemeral")
                    Path(".git").mkdir(exist_ok=True)
                    Path(".git/visible").write_text("host metadata is writable")
                    assert list(Path(".agents/console/sessions").glob("*/internal/events.jsonl"))
                    print(r.x + 1)
                    import duckdb
                    connection = duckdb.connect()
                    console_sql_connection(connection)
                    """)
            )
            assert last_result_text(client) == "read-only share enforced\n42.0\n", (
                last_result_text(client)
            )
            identity = generations(project)[0]
            inspected = sbx("exec", identity["name"], "ps", "-eo", "pid,ppid,args")
            assert (
                inspected.returncode == 0
                and "mcp-console worker-relay" in inspected.stdout
                and "mcp-console worker" in inspected.stdout
            ), inspected
            client.send(sql="SELECT 6 * 7 AS answer")
            assert "42" in last_result_text(client)
            client.send(python='print(input("VM prompt: "))')
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "input value\n", "VM input", stdin="input value\n"
            )
            client.send(r="plot(1:3)")
            session = next((project / ".agents/console/sessions").iterdir())
            artifact = next((session / "artifacts").iterdir())
            assert_result_content(
                client, [artifact.read_bytes()], image_reference="controller recording"
            )
            wait_for_evaluation_output(
                client,
                "interrupt gate\n\n[running; poll with an empty send]",
                "VM loop",
                r='cat("interrupt gate\\n"); flush.console(); repeat Sys.sleep(60)',
                timeout_ms=1,
            )
            wait_for_evaluation_output(
                client, "\n", "VM R interrupt", control="interrupt"
            )
            client.send(python="import package_that_does_not_exist_in_the_template")
            assert "ModuleNotFoundError" in last_result_text(client)
            client.send(
                r='reticulate::py_require("package_that_does_not_exist_in_the_template")'
            )
            client.send(r="library(package_that_does_not_exist_in_the_template)")
            assert "there is no package called" in last_result_text(client), (
                last_result_text(client)
            )
            client.send(requirements={"r": ["praise"]})
            assert "Docker Sandbox targets" in last_result_text(client)
            client.send(python='print(Path("/tmp/resolver-invoked").exists())')
            assert last_result_text(client) == "False\n", last_result_text(client)
            config.write_text("invalid: [")
            client.send(control="restart")
            assert (
                last_result_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), last_result_text(client)
            absent(**identity)
            client.send(
                python='from pathlib import Path; print(Path("result.txt").read_text()); print(Path("/tmp/generation-state").exists())'
            )
            assert last_result_text(client) == "persistent share\nFalse\n", (
                last_result_text(client)
            )
            client.send(r="exists('x')")
            assert last_result_text(client) == "[1] FALSE\n"
            transcript = finish(client, root)[3:]
        identities = generations(project)
        assert len(identities) == 2 and identities[0]["id"] != identities[1]["id"]
        for identity in identities:
            absent(**identity)
        assert not (root / "sentinel").exists()
        assert (project / ".git/visible").read_text() == "host metadata is writable"
        event = json.loads(
            (session / "internal/events.jsonl").read_text().splitlines()[0]
        )
        assert event["working_directory"] == str(project)
        assert (
            event["target"]["provider"] == "compute"
            and event["target"]["inner_native_runner"] is False
        )
        assert (
            event["target"]["compute"]["template_identity"]
            == os.environ["MCP_CONSOLE_TEST_SBX_TEMPLATE"]
        )
        qmd = (session / "transcript.qmd").read_text()
        frontmatter = qmd.split("---", 2)[1]
        assert "root.dir" not in qmd and "execute:" not in frontmatter, qmd
        assert "# Run `ir render transcript.qmd`" in frontmatter, qmd
        assert "Docker Sandbox" in qmd, qmd
        created = [call["args"] for call in calls(root) if call["args"][0] == "create"]
        assert len(created) == 3
        assert all(
            args[args.index("--template") + 1]
            == os.environ["MCP_CONSOLE_TEST_SBX_TEMPLATE"]
            for args in created
        )
        return json.loads(json.dumps(transcript).replace(str(root), "<sandbox-test>"))


@requires(DOCKER_SANDBOX)
def test_mountless_workspace_and_no_sandbox_keep_microvm(binary: Path) -> list:
    with workspace(real=True) as root:
        configure(root)
        with McpClient(
            binary, ("serve", "--no-sandbox"), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                python='import os; print(os.getcwd()); print(os.path.exists(".agents/console/sessions"))'
            )
            assert last_result_text(client) == "/workspace\nFalse\n", last_result_text(
                client
            )
            transcript = finish(client, root)[3:]
        for identity in generations(root):
            absent(**identity)
        return transcript


@requires(DOCKER_SANDBOX)
def test_workload_environment_does_not_configure_controller_sbx(binary: Path) -> list:
    records = []
    for inherit in (True, False):
        with workspace(real=True) as root:
            config = configure(
                root,
                environment={
                    "HOME": "/tmp/vm-workload-home",
                    "PATH": "/opt/analysis/bin:/usr/bin:/bin",
                    "R_HOME": "/usr/lib/R",
                    "RETICULATE_PYTHON": "/opt/analysis/bin/python",
                    "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "1",
                },
            )
            policy = json.loads(config.read_text())
            policy["sandbox"]["inherit_environment"] = inherit
            config.write_text(json.dumps(policy))
            with McpClient(binary, ("serve",), current_directory=root) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: python
                    python=code(f"""
                        import os, sys
                        assert sys.platform == "linux"
                        assert os.environ["HOME"] == "/tmp/vm-workload-home"
                        assert ("R_PROFILE_USER" in os.environ) == {inherit!r}
                        assert os.environ["MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION"] == "0"
                        assert os.environ["RETICULATE_USE_MANAGED_VENV"] == "no"
                        print("VM workload environment")
                        """)
                )
                assert last_result_text(client) == "VM workload environment\n", (
                    last_result_text(client)
                )
                records += finish(client, root)[3:]
            for identity in generations(root):
                absent(**identity)
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
