#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_startup import gated_discovery
from boundaries.client_server.python.test_startup import isolated_python, selected_python
from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@requires(R)
def test_accepts_zero_timeout_cell_during_discovery(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with gated_discovery(binary, r_home=Path(environment["R_HOME"])) as (client, release):
        client.initialize_and_list_tools()
        client.send(r='started <- get0("started", ifnotfound = 0L) + 1L; started', timeout_ms=0)
        assert last_result_text(client) == RUNNING
        client.send(timeout_ms=0)
        assert last_result_text(client) == RUNNING
        client.send(r="stop('second cell must not execute')", timeout_ms=0)
        assert client.transcript[-1]["result"]["isError"]
        client.request("ping")
        release.release()
        client.send()
        assert last_result_text(client) == "[1] 1\n"
        client.send(r="started")
        assert last_result_text(client) == "[1] 1\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_python_before_any_send(binary: Path, execution: Execution) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory())).resolve()
        reached = FifoCheckpoint.create(root / "reached")
        release = FifoCheckpoint.create(root / "release")
        resources.callback(reached.close)
        resources.callback(release.close)
        python, site = isolated_python(root)
        # Resolver probes use -c. Only the embedded interpreter owns this gate.
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import os
                import sys

                if sys.argv[0] != "-c":
                    with open({str(root / 'pid')!r}, "w") as stream:
                        stream.write(str(os.getpid()))
                    with open({str(reached.path)!r}, "wb", buffering=0) as stream:
                        stream.write(b"1")
                    with open({str(release.path)!r}, "rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                """),
        )
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment["PATH"] = str(root)
        environment["PYTHONPATH"] = str(site)
        with McpClient(binary, execution.serve(), environment, root, response_timeout=5) as client:
            # Release before connection teardown if an assertion fails.
            try:
                reached.wait("embedded Python starts without a send")
                client.initialize_and_list_tools()
                tools = client.transcript[-1]["result"]
                client.request("ping")
                client.send(python="import os\nos.getpid()", timeout_ms=0)
                assert last_result_text(client) == RUNNING
                release.release()
                client.send()
                assert last_result_text(client) == (root / "pid").read_text() + "\n"
                client.transcript[-1]["result"]["content"][0]["text"] = "<prewarmed worker pid>\n"
                assert client.request("tools/list")["result"] == tools
                client.transcript[-1]["result"] = "<unchanged from initialization>"
                client.send(python="os.getpid()")
                assert last_result_text(client) == (root / "pid").read_text() + "\n"
                client.transcript[-1]["result"]["content"][0]["text"] = "<same worker pid>\n"
                return client.finish()
            finally:
                release.release()


if __name__ == "__main__":
    run_this_suite(__file__)
