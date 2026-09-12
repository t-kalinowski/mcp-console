#!/usr/bin/env -S uv run --script
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.docker import (
    DOCKER,
    absent,
    cli_peer,
    configure,
    docker,
    image,
    native_sandbox_available,
    workspace,
)
from support.records import Transcript
from support.normalization import code
from support.requirements import requires
from support.suites import run_this_suite


@requires(DOCKER)
def test_native_selection_is_enforced_or_rejected(binary: Path) -> Transcript:
    reference = image()
    capable = native_sandbox_available(reference)
    with workspace() as root:
        environment = cli_peer(root / "peer")
        (root / ".claude").mkdir()
        (root / "allowed").mkdir()
        (root / "readonly").mkdir()
        config = configure(
            root,
            reference,
            mounts=[
                {"source": ".", "target": "/workspace", "access": "read_write"},
                {
                    "source": "allowed",
                    "target": "/allowed-writes",
                    "access": "read_write",
                },
            ],
        )
        policy = json.loads(config.read_text())
        policy["extends"] = ":workspace"
        policy["sandbox"] = {
            "network": "enabled",
            "filesystem": {
                "entries": [
                    {
                        "path": {"type": "path", "path": "/workspace/readonly"},
                        "access": "read",
                    }
                ]
            },
        }
        config.write_text(json.dumps(policy))
        with McpClient(
            binary,
            ("serve", "--writable-root", "/allowed-writes"),
            environment,
            root,
        ) as client:
            if capable:
                client.initialize_and_list_tools()
                client.send(
                    python=code("""
                    from pathlib import Path
                    Path("result").write_text("yes")
                    Path("/allowed-writes/allowed").write_text("yes")
                    for denied in (".claude/denied", "readonly/denied"):
                        try:
                            Path(denied).write_text("no")
                        except PermissionError:
                            print("write denied")
                        else:
                            raise AssertionError("protected write succeeded")
                """)
                )
                assert last_result_text(client) == "write denied\nwrite denied\n", (
                    last_result_text(client)
                )
                client.finish()
            else:
                assert client.stdout.read(timeout=30) == ""
                error = client.stderr.read(timeout=30)
                assert "bwrap:" in error or "mcp-console-sandbox:" in error, error
                assert client.process.wait(timeout=5) != 0
                print(error, file=sys.stderr, end="")
        for line in (root / "peer/calls").read_text().splitlines():
            args = json.loads(line)["args"]
            if "create" in args:
                assert not any(
                    arg in args
                    for arg in ("--privileged", "--cap-add", "--security-opt")
                ), args
                absent(args[args.index("--name") + 1])
        # The same contract holds on capable and constrained daemons. Test
        # support gates permission assertions with an actual native probe.
        return [
            {
                "native_selection_preserved": True,
                "permission_checks_require_native_capability": True,
                "no_security_downgrade": True,
                "containers_absent": True,
            }
        ]


@requires(DOCKER)
def test_delegated_environment_and_no_sandbox(binary: Path) -> Transcript:
    reference = image()
    records = []
    for arguments in (("serve",), ("serve", "--no-sandbox")):
        with workspace() as root:
            config = configure(
                root,
                reference,
                environment={
                    "PATH": "/opt/analysis/bin:/usr/bin:/bin",
                    "R_HOME": "/usr/lib/R",
                    "RETICULATE_PYTHON": "/opt/analysis/bin/python",
                    "WORKLOAD_VALUE": "selected",
                    "DOCKER_HOST": "must-not-configure-the-launcher",
                },
                compute={
                    "kind": "docker",
                    "image": reference,
                    "pull": "never",
                    "user": "4321:4321",
                },
            )
            policy = json.loads(config.read_text())
            policy["sandbox"]["inherit_environment"] = False
            policy["sandbox"]["network"] = "restricted"
            # Native filesystem entries do not narrow external-sandbox.
            policy["sandbox"]["filesystem"]["entries"] = [
                {"path": {"type": "path", "path": "/tmp"}, "access": "read"}
            ]
            config.write_text(json.dumps(policy))
            with McpClient(
                binary,
                arguments,
                {
                    **os.environ,
                    "HOME": "/controller-home",
                    "TMPDIR": "/controller-temp",
                },
                root,
            ) as client:
                client.initialize_and_list_tools()
                description = client.transcript[-1]["result"]["tools"][0]["description"]
                assert "cannot directly access the network" not in description
                assert "with the server's permissions" not in description
                client.send(
                    python=code("""
                    import os
                    from pathlib import Path
                    assert os.getuid() == 4321
                    assert os.environ["WORKLOAD_VALUE"] == "selected"
                    assert os.environ["DOCKER_HOST"] == "must-not-configure-the-launcher"
                    assert os.environ.get("HOME") != "/controller-home"
                    assert os.environ.get("TMPDIR") != "/controller-temp"
                    Path("/tmp/delegated-write").write_text("yes")
                    print("container environment")
                """)
                )
                assert last_result_text(client) == "container environment\n", (
                    last_result_text(client)
                )
                wait_for_evaluation_output(
                    client,
                    "there is no package called 'mcpConsoleDefinitelyMissingPackage'\n",
                    "missing preinstalled R package",
                    r='tryCatch(library(mcpConsoleDefinitelyMissingPackage), error=function(error) cat(conditionMessage(error), "\\n", sep=""))',
                )
                wait_for_evaluation_output(
                    client,
                    "No module named 'mcpConsoleDefinitelyMissingPackage'.\n\nMCP Console dynamic environment resolution is unavailable for Docker targets. Install the distribution in the image and start a new server session.\n",
                    "missing preinstalled Python package",
                    python=code("""
                        try:
                            import mcpConsoleDefinitelyMissingPackage
                        except ModuleNotFoundError as error:
                            print(error)
                    """),
                )
                transcript = client.finish()[3:]
            records.append(
                {
                    "arguments": list(arguments),
                    "target_environment": True,
                    "delegated_filesystem": True,
                    "preinstalled_packages_required": True,
                }
            )
            records.extend(transcript)
    return records


@requires(DOCKER)
def test_explicit_proxy_uses_native_setup(binary: Path) -> Transcript:
    reference = image()
    with workspace() as root:
        config = configure(root, reference)
        policy = json.loads(config.read_text())
        policy["sandbox"]["proxy"] = {
            "enabled": True,
            "enableSocks5": True,
            "enableSocks5Udp": False,
            "allowUpstreamProxy": False,
            "dangerouslyAllowAllUnixSockets": False,
            "allowLocalBinding": False,
            "mode": "full",
            "domains": {},
        }
        config.write_text(json.dumps(policy))
        with McpClient(binary, ("serve",), current_directory=root) as client:
            entry = client.start_request(
                "initialize",
                protocolVersion="2025-11-25",
                capabilities={},
                clientInfo={"name": "docker-proxy-test", "version": "1"},
            )
            line = client.stdout.readline(timeout=30)
            if line:
                response = json.loads(line)
                assert response["id"] == entry["id"] and "result" in response, response
                client.notify("notifications/initialized")
                client.send(
                    python='import os; assert os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"); print("native proxy configured")'
                )
                assert last_result_text(client) == "native proxy configured\n", (
                    last_result_text(client)
                )
                client.finish()
            else:
                error = client.stderr.read(timeout=15)
                assert "bwrap:" in error or "mcp-console-sandbox:" in error, error
                assert client.process.wait(timeout=5) != 0
                print(error, file=sys.stderr, end="")
        return [
            {
                "explicit_proxy_uses_native_setup": True,
                "native_failure_is_not_downgraded": True,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
