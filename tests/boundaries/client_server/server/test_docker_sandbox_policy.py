#!/usr/bin/env -S uv run --script
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.docker_sandbox import (
    DOCKER_SANDBOX,
    DOCKER_SANDBOX_NETWORK,
    calls,
    cli_peer,
    configure,
    finish,
    generations,
    normalize_recording,
    sbx,
    workspace,
)
from support.normalization import code
from support.requirements import requires
from support.suites import run_this_suite


@requires(DOCKER_SANDBOX, DOCKER_SANDBOX_NETWORK)
def test_inherited_allow_and_owned_rules_preserve_global_policy(binary: Path) -> list:
    def global_rules():
        result = sbx("policy", "ls", "--json", "--include-inactive")
        assert result.returncode == 0, result.stderr
        return [
            rule
            for rule in json.loads(result.stdout)["rules"]
            if rule["scope"] == "global"
        ]

    with workspace(real=True) as root:
        before = global_rules()
        unrelated = json.loads(sbx("ls", "--json").stdout)["sandboxes"]
        configure(root)
        with McpClient(binary, ("serve",), current_directory=root) as client:
            client.initialize_and_list_tools()
            client.send(
                python=code("""
                import urllib.request, urllib.error
                def fetch(host):
                    try:
                        with urllib.request.urlopen("https://" + host, timeout=15) as response:
                            print(host, response.status)
                    except urllib.error.URLError as error:
                        print(host, type(error).__name__, str(error))
                fetch("pypi.org")
            """)
            )
            assert last_result_text(client) == "pypi.org 200\n", last_result_text(
                client
            )
            identity = generations(root)[0]
            allowed = sbx(
                "policy",
                "allow",
                "network",
                "--sandbox",
                identity["name"],
                "example.com:443",
            )
            assert allowed.returncode == 0, allowed.stderr
            client.send(python='fetch("example.com"); fetch("registry.npmjs.org")')
            assert (
                last_result_text(client) == "example.com 200\nregistry.npmjs.org 200\n"
            ), last_result_text(client)
            denied = sbx(
                "policy",
                "deny",
                "network",
                "--sandbox",
                identity["name"],
                "pypi.org:443",
            )
            assert denied.returncode == 0, denied.stderr
            checked = json.loads(
                sbx(
                    "policy",
                    "check",
                    "network",
                    "--json",
                    "--sandbox",
                    identity["name"],
                    "pypi.org:443",
                ).stdout
            )
            assert checked["allowed"] is False, checked
            client.send(python='fetch("pypi.org"); fetch("registry.npmjs.org")')
            assert "403" in last_result_text(client) and last_result_text(
                client
            ).endswith("registry.npmjs.org 200\n"), last_result_text(client)
            transcript = finish(client, root)[3:]
        assert global_rules() == before
        after = json.loads(sbx("ls", "--json").stdout)["sandboxes"]
        for previous in unrelated:
            assert previous in after, (previous, after)
        return transcript + [
            {
                "global_rules_unchanged": True,
                "unrelated_resources_unchanged": True,
                "owned_allow_is_additive": True,
                "owned_deny_overrides_inherited_allow": True,
            }
        ]


@requires(DOCKER_SANDBOX)
def test_provider_reports_missing_paths_and_runtime_readiness(binary: Path) -> list:
    records = []
    for failure in (
        "missing-workspace",
        "missing-share",
        "readonly-primary",
        "missing-console",
    ):
        with workspace(real=True) as root:
            environment = cli_peer(root / "peer", real=True)
            options = {}
            if failure == "missing-workspace":
                options["workspace"] = "/console-workspace-does-not-exist"
            elif failure == "missing-share":
                options["mounts"] = [
                    {
                        "source": str(root / "missing"),
                        "target": str(root / "missing"),
                        "access": "read_write",
                    }
                ]
            elif failure == "readonly-primary":
                options["mounts"] = [
                    {"source": str(root), "target": str(root), "access": "read_only"}
                ]
            else:
                options["command"] = ["/console-executable-does-not-exist"]
            configure(root, **options)
            with McpClient(binary, ("serve",), environment, root) as client:
                assert client.stdout.read(timeout=35) == ""
                errors = client.stderr.read(timeout=20)
                assert client.process.wait(timeout=5) != 0
            assert errors, failure
            if failure == "missing-workspace":
                assert options["workspace"] in errors, errors
            elif failure == "missing-share":
                assert str(root / "missing") in errors, errors
            elif failure == "missing-console":
                assert options["command"][0] in errors, errors
            invoked = calls(root)
            assert not any(
                call["args"][-1] == "docker-sandbox-launch" for call in invoked
            ), invoked
            names = {
                call["args"][call["args"].index("--name") + 1]
                for call in invoked
                if call["args"][0] == "create"
            }
            listed = sbx("ls", "--json")
            assert listed.returncode == 0, listed.stderr
            assert not any(
                vm["name"] in names for vm in json.loads(listed.stdout)["sandboxes"]
            ), listed.stdout
            records += normalize_recording(
                [{"failure": failure, "stderr": errors}], root
            )
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
