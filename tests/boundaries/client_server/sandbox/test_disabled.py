#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient, stop_client
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
)
from support.normalization import code
from support.records import Transcript, TranscriptWithCompanions
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


def _direct_generation(client: McpClient, binary: Path) -> tuple[ProcessIdentity, ...]:
    server = capture_process_identity(client.process.pid)
    children = child_process_identities(server)
    assert len(children) == 1, children
    relay = children[0]
    command = subprocess.run(
        ["/bin/ps", "-ww", "-o", "args=", "-p", str(relay[0])],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert command.startswith(f"{binary} worker-relay "), command
    children = child_process_identities(relay)
    assert len(children) == 1, children
    worker = children[0]
    return relay, worker


def test_builtin_worker_runs_directly_with_host_access(
    binary: Path,
) -> Transcript | TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(workspace)
        client = McpClient(
            binary,
            ("serve", "--no-sandbox"),
            environment,
            current_directory=workspace,
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            description = client.transcript[-1]["result"]["tools"][0]["description"]
            assert "without a sandbox" in description, description
            assert "server's permissions" in description, description
            client.send(
                python=code(r"""
                    import os
                    from pathlib import Path

                    assert Path(os.environ["TMPDIR"]).samefile(Path.cwd())
                    output = Path("host-output")
                    output.write_text("host write succeeded", encoding="utf-8")
                    print(output.read_text(encoding="utf-8"))
                    """)
            )
            assert (workspace / "host-output").read_text() == "host write succeeded"
            identities.extend(_direct_generation(client, binary))
            client.send(
                control="restart",
                python='print("output" in globals())',
            )
            assert "False\n" in last_tool_text(client), client.transcript[-1]
            assert live_processes(identities) == []
            identities.extend(_direct_generation(client, binary))
            client.finish()
            assert live_processes(identities) == []
            transcript = client.transcript
            if sys.platform == "linux":
                return TranscriptWithCompanions(transcript, {}, platform="linux")
            return transcript
        finally:
            stop_client(client)
            kill_processes(identities)


def test_custom_worker_runs_directly_with_host_access(
    binary: Path,
) -> Transcript | TranscriptWithCompanions:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "host-output"
        relay = Path(directory) / "relay-wrapper"
        relay.write_text(
            "#!/usr/bin/env python3\n"
            + code(r"""
                import os
                import sys

                binary = os.environ["MCP_CONSOLE_TEST_BINARY"]
                os.execv(binary, [binary, "worker-relay", *sys.argv[1:]])
                """),
            encoding="utf-8",
        )
        relay.chmod(0o755)
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["ZOD_SANDBOX_PROBE_PATH"] = str(output)
        client = McpClient(
            binary,
            (
                "serve",
                "--no-sandbox",
                "--worker",
                str(zod),
                "--relay",
                str(relay),
            ),
            environment,
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            client.send(r="probe sandbox")
            assert output.read_text() == "escaped"
            identities.extend(_direct_generation(client, binary))
            client.send(control="restart", r="echo replacement ready")
            assert "zod: replacement ready\n" in last_tool_text(client)
            assert live_processes(identities) == []
            identities.extend(_direct_generation(client, binary))
            client.finish()
            assert live_processes(identities) == []
            transcript = client.transcript
            if sys.platform == "linux":
                return TranscriptWithCompanions(transcript, {}, platform="linux")
            return transcript
        finally:
            stop_client(client)
            kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
