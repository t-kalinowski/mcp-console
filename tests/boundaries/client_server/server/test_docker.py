#!/usr/bin/env -S uv run --script
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
from support.docker import DOCKER, absent, configure, docker, image, workspace
from support.normalization import code
from support.records import Transcript
from support.requirements import requires
from support.suites import run_this_suite


@requires(DOCKER)
def test_persistent_image_runtime_and_controller_records(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        project = root / 'project space, "quoted"'
        project.mkdir()
        readonly = root / "read only"
        readonly.mkdir()
        (readonly / "value").write_text("read-only data")
        config = configure(
            project,
            reference,
            mounts=[
                {"source": ".", "target": "/workspace", "access": "read_write"},
                {"source": str(readonly), "target": '/read only, "quoted"'},
            ],
            environment={
                "CONSOLE_LITERAL": 'space, "quotes" $HOME',
                "MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION": "1",
            },
        )
        environment = {
            **os.environ,
            "R_HOME": "/controller-r-must-not-be-used",
            "RETICULATE_PYTHON": "/controller-python-must-not-be-used",
        }
        with McpClient(binary, ("serve",), environment, project) as client:
            client.initialize_and_list_tools()
            tool = client.transcript[-1]["result"]["tools"][0]
            assert "requirements" not in tool["inputSchema"]["properties"], tool
            client.send(
                r='x <- 41; stopifnot(getwd() == "/workspace", file.exists("/.dockerenv")); x + 1'
            )
            assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            client.send(
                python=code("""
                import os, sys, json
                from pathlib import Path
                assert sys.executable == os.environ["RETICULATE_PYTHON"]
                assert os.environ["MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION"] == "0"
                assert os.environ["RETICULATE_USE_MANAGED_VENV"] == "no"
                assert Path('/read only, "quoted"/value').read_text() == "read-only data"
                try:
                    Path('/read only, "quoted"/forbidden').write_text("bad")
                except OSError:
                    print("read-only mount enforced")
                else:
                    raise AssertionError("read-only mount was writable")
                Path("result.txt").write_text("persistent bind")
                Path("/tmp/layer-state").write_text("ephemeral layer")
                print(r.x + 1)
                """)
            )
            assert "read-only mount enforced\n42" in last_result_text(client), (
                last_result_text(client)
            )
            client.send(sql="SELECT 6 * 7 AS answer")
            assert "42" in last_result_text(client), last_result_text(client)
            client.send(python='print(input("container prompt: "))')
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "input value\n", "container input", stdin="input value\n"
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
                "container loop",
                r='cat("interrupt gate\\n"); flush.console(); repeat Sys.sleep(60)',
                timeout_ms=1,
            )
            wait_for_evaluation_output(
                client, "\n", "container R interrupt", control="interrupt"
            )
            client.send(r='gctorture(TRUE); stop("container expected error")')
            assert last_result_text(client) == "Error: container expected error\n"
            client.send(r="gctorture(FALSE); x")
            assert last_result_text(client).endswith("[1] 41\n"), last_result_text(
                client
            )
            client.send(requirements={"r": ["praise"]})
            assert "dynamic environment resolution" in last_result_text(client)
            client.send(python='print(Path("/etc/hostname").read_text().strip())')
            container = last_result_text(client).strip()
            inspected = docker("inspect", "--format", "{{json .}}", container)
            assert inspected.returncode == 0, inspected.stderr
            state = json.loads(inspected.stdout)
            assert state["HostConfig"]["Privileged"] is False
            assert state["HostConfig"]["NetworkMode"] == "bridge"
            assert state["HostConfig"]["Init"] is True
            assert state["Config"]["Tty"] is False
            processes = docker("top", container, "-eo", "pid,args")
            assert (
                "worker-relay" in processes.stdout
                and "mcp-console worker" in processes.stdout
            ), processes
            config.write_text("invalid: [")
            client.send(control="restart")
            absent(container)
            client.send(
                python='from pathlib import Path; print(Path("result.txt").read_text()); print(Path("/tmp/layer-state").exists())'
            )
            assert last_result_text(client) == "persistent bind\nFalse\n", (
                last_result_text(client)
            )
            client.send(r="exists('x')")
            assert last_result_text(client) == "[1] FALSE\n", last_result_text(client)
            client.send(python='print(Path("/etc/hostname").read_text().strip())')
            replacement = last_result_text(client).strip()
            transcript = client.finish()
        absent(replacement)
        assert (project / "result.txt").read_text() == "persistent bind"
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        event = events[0]
        assert event["working_directory"] == str(project)
        assert event["target"]["compute"]["kind"] == "docker"
        assert event["target"]["compute"]["image_id"] == reference
        assert event["target"]["workspace"] == "/workspace"
        generations = [
            event["container_id"] for event in events if "container_id" in event
        ]
        assert len(generations) == 2
        assert generations[0].startswith(container)
        assert generations[1].startswith(replacement)
        qmd = (session / "transcript.qmd").read_text()
        assert "root.dir" not in qmd and "eval: false" in qmd and "Docker" in qmd, qmd
        result = json.dumps(transcript[3:]).replace(str(root), "<docker-test>")
        for identity in (container, replacement):
            result = result.replace(identity, "<container>")
        return json.loads(result)


if __name__ == "__main__":
    run_this_suite(__file__)
