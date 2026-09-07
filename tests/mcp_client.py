from __future__ import annotations

import os
import select
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from support.client import McpClient

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
elif mode == "context":
    request = json.loads(sys.stdin.readline())
    response = {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {"content": [{"type": "text", "text": "ready"}], "isError": False},
    }
    print(json.dumps(response), flush=True)
    assert sys.stdin.read() == ""
    (root / "stdin-closed").touch()
    raise SystemExit(0)
else:
    raise AssertionError(mode)

with (root / "release").open("rb", buffering=0) as release:
    assert release.read(1) == b"1"
""".lstrip()


@unittest.skipUnless(os.name == "posix", "requires POSIX FIFO APIs")
class McpClientTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
