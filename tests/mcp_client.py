from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from support.client import McpClient
from support.events import Events
from support.processes import capture_process_identity, signal_process
from support.requirements import POSIX, PROCESS_EVENTS

ROOT = Path(__file__).resolve().parent.parent

# fmt: python
FAKE_SERVER = r"""
import json
import os
import sys
from pathlib import Path

mode = sys.argv[1]
root = Path(sys.argv[2])
with (root / "ready").open("wb", buffering=0) as ready:
    assert ready.write(b"1") == 1

if mode == "partial":
    json.loads(sys.stdin.readline())
    os.write(2, b"partial response diagnostic\n")
    os.write(1, b'{"jsonrpc":"2.0","id":')
elif mode == "finish":
    assert sys.stdin.read() == ""
    os.write(2, b"shutdown diagnostic\n")
elif mode in {"context", "refuse-close", "late-response"}:
    request = json.loads(sys.stdin.readline())
    response = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"content": [{"type": "text", "text": "ready"}], "isError": False},
    }
    print(json.dumps(response), flush=True)
    if mode == "late-response":
        json.loads(sys.stdin.readline())
        os.write(2, b"partial response diagnostic\n")
        os.write(1, b'{"jsonrpc":"2.0","id":')
    else:
        assert sys.stdin.read() == ""
        (root / "stdin-closed").touch()
        if mode == "context":
            raise SystemExit(0)
        os.write(2, b"server refuses input closure\n")
        with (root / "shutdown-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
else:
    raise AssertionError(mode)

with (root / "release").open("rb", buffering=0) as release:
    assert release.read(1) == b"1"
""".lstrip()

# fmt: python
RUNNER_CLIENT_SUITE = """
import sys
from pathlib import Path

from support.client import McpClient


def test_waits_with_client(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    client = McpClient(
        Path(sys.executable),
        ("-u", str(root / "server.py"), "refuse-close", str(root)),
        current_directory=root,
    )
    (root / "server-pid").write_text(str(client.process.pid))
    try:
        with client:
            assert client.send(r="1")["content"][0]["text"] == "ready"
            with (root / "case-started").open("wb", buffering=0) as started:
                assert started.write(b"1") == 1
            with (root / "case-release").open("rb", buffering=0) as release:
                assert release.read(1) == b"1"
    finally:
        (root / "client-closed").write_text(str(client.process.returncode))
        with (root / "case-cleanup-complete").open("wb", buffering=0) as cleaned:
            assert cleaned.write(b"1") == 1
    return []
""".lstrip()

# fmt: python
LATE_RESPONSE_SUITE = """
import sys
from pathlib import Path

from support.client import McpClient


def test_waits_with_client(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    client = McpClient(
        Path(sys.executable),
        ("-u", str(root / "server.py"), "late-response", str(root)),
        current_directory=root,
        response_timeout=60,
        shutdown_timeout=0.1,
    )
    try:
        with client:
            assert client.send(r="1")["content"][0]["text"] == "ready"
            with (root / "case-started").open("wb", buffering=0) as started:
                assert started.write(b"1") == 1
            client.send(r="2")
    finally:
        with (root / "case-cleanup-complete").open("wb", buffering=0) as cleaned:
            assert cleaned.write(b"1") == 1
    return []
""".lstrip()


@unittest.skipUnless(POSIX.available, POSIX.reason)
class McpClientTests(unittest.TestCase):
    @contextmanager
    def client_runner(
        self, suite: str, *arguments: str
    ) -> Iterator[tuple[subprocess.Popen[str], Path, list[int]]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = root / "tests" / "boundaries" / "_run.py"
            suite_path = runner.parent / "client_server" / "server" / "test_client.py"
            snapshots = (
                root
                / "tests"
                / "snapshots"
                / "client_server"
                / "server"
                / "test_client"
            )
            support = root / "tests" / "support"
            binary = root / "target" / "release" / "mcp-console"
            for path in (suite_path.parent, snapshots, support, binary.parent):
                path.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / "tests" / "boundaries" / "_run.py", runner)
            for name in (
                "__init__.py",
                "cases.py",
                "client.py",
                "records.py",
                "snapshots.py",
                "requirements.py",
                "execution.py",
            ):
                shutil.copy2(ROOT / "tests" / "support" / name, support / name)
            binary.touch()
            suite_path.write_text(suite)
            (snapshots / "waits_with_client.yaml").write_text("[]\n")
            (root / "server.py").write_text(FAKE_SERVER)
            checkpoints = []
            for name in (
                "ready",
                "release",
                "case-started",
                "case-release",
                "shutdown-started",
                "case-cleanup-complete",
            ):
                os.mkfifo(root / name)
                checkpoints.append(os.open(root / name, os.O_RDWR | os.O_NONBLOCK))
            process = subprocess.Popen(
                ["uv", "run", "--script", runner, "--jobs", "1", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                cwd=root,
            )
            try:
                yield process, root, checkpoints
            finally:
                try:
                    os.write(checkpoints[1], b"1")
                    os.write(checkpoints[3], b"1")
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=5)
                    ready, _, _ = select.select([checkpoints[5]], [], [], 5)
                    self.assertTrue(ready, "case did not finish its client cleanup")
                    self.assertEqual(os.read(checkpoints[5], 1), b"1")
                finally:
                    for checkpoint in checkpoints:
                        os.close(checkpoint)

    @contextmanager
    def fake_client(
        self, mode: str, **timeouts: float
    ) -> Iterator[tuple[McpClient, Path]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoints = []
            for name in ("ready", "release"):
                os.mkfifo(root / name)
                checkpoints.append(os.open(root / name, os.O_RDWR | os.O_NONBLOCK))
            client = McpClient(
                Path(sys.executable),
                ("-u", "-c", FAKE_SERVER, mode, str(root)),
                current_directory=root,
                **timeouts,
            )
            try:
                ready, _, _ = select.select([checkpoints[0]], [], [], 10)
                self.assertTrue(ready, "fake server did not start")
                self.assertEqual(os.read(checkpoints[0], 1), b"1")
                yield client, root
            finally:
                os.write(checkpoints[1], b"1")
                if client.process.poll() is None:
                    client.process.kill()
                client.process.wait(timeout=5)
                for stream in (client.stdin, client.stdout, client.stderr):
                    stream.close()
                for checkpoint in checkpoints:
                    os.close(checkpoint)

    def call_bounded(self, client: McpClient, call: Callable[[], object]) -> object:
        def capture() -> object:
            try:
                return call()
            except (AssertionError, RuntimeError, TimeoutError) as error:
                return error

        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(capture)
            try:
                return result.result(timeout=5)
            finally:
                # Killing this fixture closes its partial streams before the
                # executor joins, so a broken client cannot hang this test.
                if not result.done():
                    client.process.kill()
                    client.process.wait(timeout=5)

    def test_partial_response_times_out_with_server_diagnostics(self) -> None:
        with self.fake_client("partial", response_timeout=1, shutdown_timeout=1) as (
            client,
            _,
        ):
            result = self.call_bounded(client, lambda: client.send(r="1"))
            self.assertIsInstance(result, TimeoutError)
            self.assertIn("response", str(result))
            self.assertIn("partial response diagnostic", str(result))

    def test_finish_times_out_with_server_diagnostics(self) -> None:
        with self.fake_client("finish", shutdown_timeout=1) as (client, _):
            result = self.call_bounded(client, client.finish)
            self.assertIsInstance(result, TimeoutError)
            self.assertIn("shutdown", str(result))
            self.assertIn("shutdown diagnostic", str(result))

    def test_context_exception_closes_stdin_and_reaps_server(self) -> None:
        with self.fake_client("context", shutdown_timeout=1) as (client, root):

            def fail_in_context() -> None:
                with client:
                    result = client.send(r="1")
                    self.assertEqual(
                        result["content"], [{"type": "text", "text": "ready"}]
                    )
                    raise AssertionError("scenario assertion failed")

            result = self.call_bounded(client, fail_in_context)
            self.assertIsInstance(result, AssertionError)
            self.assertEqual(str(result), "scenario assertion failed")
            self.assertEqual(client.process.returncode, 0)
            self.assertTrue((root / "stdin-closed").is_file())

    @unittest.skipUnless(PROCESS_EVENTS.available, PROCESS_EVENTS.reason)
    def test_runner_interrupt_allows_client_to_reap_unresponsive_server(self) -> None:
        with self.client_runner(RUNNER_CLIENT_SUITE, "--timeout", "60") as (
            process,
            root,
            checkpoints,
        ):
            identity = None
            exits = Events()
            try:
                ready, _, _ = select.select([checkpoints[2]], [], [], 10)
                self.assertTrue(ready, "case did not receive its server response")
                self.assertEqual(os.read(checkpoints[2], 1), b"1")
                pid = int((root / "server-pid").read_text())
                identity = capture_process_identity(pid)
                exits.watch_process(pid)
                process.send_signal(signal.SIGINT)
                ready, _, _ = select.select([checkpoints[4]], [], [], 10)
                self.assertTrue(ready, "case did not close server stdin")
                self.assertEqual(os.read(checkpoints[4], 1), b"1")
                # Server exit must leave room for the case to finish before
                # its supervisor's 15-second forced-cleanup deadline.
                observed = exits.wait(14)
                self.assertTrue(observed, "client left no time for case cleanup")
                self.assertEqual(observed, {pid})
                stdout, stderr = process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0, stdout)
                self.assertEqual((root / "client-closed").read_text(), "-9")
                self.assertIn("KeyboardInterrupt", stderr)
            finally:
                if identity is not None:
                    signal_process(identity, signal.SIGKILL)
                exits.close()

    def test_later_response_reports_diagnostics_before_runner_deadline(self) -> None:
        with self.client_runner(LATE_RESPONSE_SUITE, "--timeout", "16") as (
            process,
            _,
            checkpoints,
        ):
            ready, _, _ = select.select([checkpoints[2]], [], [], 10)
            self.assertTrue(ready, "first response did not complete")
            self.assertEqual(os.read(checkpoints[2], 1), b"1")
            stdout, stderr = process.communicate(timeout=20)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn("timed out waiting for response", stderr)
            self.assertIn("partial response diagnostic", stderr)
            self.assertNotIn("timed out after 16 seconds", stderr)


if __name__ == "__main__":
    unittest.main()
