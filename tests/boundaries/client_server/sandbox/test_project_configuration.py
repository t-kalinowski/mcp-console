#!/usr/bin/env -S uv run --script

import json
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.suites import run_this_suite


def _snapshot_survives_replacement(
    binary: Path, configured: bool
) -> TranscriptWithCompanions:
    exercise = code(r"""
        import errno
        import os
        from pathlib import Path
        import socket

        host = Path(os.environ["MCP_CONSOLE_TEST_PROJECT"])
        configured = os.environ["MCP_CONSOLE_TEST_CONFIGURED"] == "1"
        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert (os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1") == configured
        _ = (Path(os.environ["TMPDIR"]) / "private").write_text("private storage")
        _ = (host / "CLI cache" / "persistent").write_text("CLI grant")
        for name, allowed in (("output café 雪", configured), ("neighbor", False)):
            try:
                _ = (host / name / "created").write_text("project grant")
            except OSError as error:
                assert not allowed and error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                assert allowed, name
        try:
            socket.create_connection(("127.0.0.1", int(os.environ["MCP_CONSOLE_TEST_PORT"])), timeout=2)
        except OSError:
            pass
        else:
            raise AssertionError("direct network unexpectedly allowed")
        os.chdir(host / "CLI cache")
        print("captured grants, proxy selection, and restricted network verified")
        """)
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory).resolve()
        for name in ("output café 雪", "CLI cache", "neighbor"):
            (host / name).mkdir()
        config = host / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        if configured:
            config.write_text(
                code("""
                sandbox:
                  filesystem:
                    entries:
                      - path: {type: path, path: ./output café 雪}
                        access: write
                  proxy:
                    enabled: true
                    domains: {127.0.0.1: allow}
                """),
                encoding="utf-8",
            )
        capture = host / "payloads.jsonl"
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(host, "runner_configuration")),
            "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(capture),
            "MCP_CONSOLE_TEST_PROJECT": str(host),
            "MCP_CONSOLE_TEST_CONFIGURED": str(int(configured)),
            "MCP_CONSOLE_SANDBOX_SETTINGS": "invalid ambient settings",
        }
        environment.pop("RETICULATE_PYTHON", None)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        environment["MCP_CONSOLE_TEST_PORT"] = str(listener.getsockname()[1])
        expected = "captured grants, proxy selection, and restricted network verified\n"
        with McpClient(
            binary, ("serve", "--writable-root", "CLI cache"), environment, host
        ) as client:
            client.initialize_and_list_tools()
            # Configured startup probes the native sandbox without starting a worker.
            preflights = capture.read_text().splitlines() if capture.exists() else []
            assert len(preflights) == int(configured), preflights
            # Even the first worker uses the snapshot taken before MCP readiness.
            config.write_text("sandbox: {network: enabled}\n", encoding="utf-8")
            client.send(python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            config.write_text("invalid: [", encoding="utf-8")
            client.send(control="restart")
            client.send(python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            config.unlink()
            client.send(python="os._exit(23)")
            client.send(python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)

            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
            )
            assert (
                last_tool_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), last_tool_text(client)
            client.send(python=exercise)
            assert last_tool_text(client).endswith(expected), last_tool_text(client)
            client.send(python="import yaml12; print(yaml12.__name__)")
            assert last_tool_text(client) == "yaml12\n", last_tool_text(client)
            transcript = client.finish()

        payloads = [json.loads(line) for line in capture.read_text().splitlines()]
        assert len(payloads) == 4 + len(preflights), len(payloads)
        assert all(payload == payloads[0] for payload in payloads), payloads
        payload = payloads[0]
        assert payload["network"] == "restricted"
        assert (payload["proxy"] is not None) == configured
        expected_roots = (
            ["output café 雪", "CLI cache"] if configured else ["CLI cache"]
        )
        assert payload["filesystem"]["entries"][1:] == [
            {"path": {"type": "path", "path": str(host / name)}, "access": "write"}
            for name in expected_roots
        ], payload
        assert payload["lifecycle"] == {
            "parent_pid": client.process.pid,
            "sigterm": "retire",
            "private_tmp": {"environment": ["TMPDIR"]},
            "cleanup_timeout_ms": 1000,
        }, payload
        assert not (host / "neighbor/created").exists()
        return TranscriptWithCompanions(
            transcript,
            {
                "settings.yaml": [
                    {
                        "initially_configured": configured,
                        "validation_launches": len(preflights),
                        "identical_worker_launches": len(payloads) - len(preflights),
                        "writable_roots": expected_roots,
                        "network": payload["network"],
                        "proxy": payload["proxy"],
                    }
                ]
            },
        )


@requires(SANDBOX, NATIVE_FIXTURES)
def test_retains_project_settings_after_edits(binary: Path) -> TranscriptWithCompanions:
    return _snapshot_survives_replacement(binary, configured=True)


@requires(SANDBOX, NATIVE_FIXTURES)
def test_retains_defaults_after_config_creation(
    binary: Path,
) -> TranscriptWithCompanions:
    return _snapshot_survives_replacement(binary, configured=False)


if __name__ == "__main__":
    run_this_suite(__file__)
