from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SandboxInstallationTests(unittest.TestCase):
    binary_source: Path
    runner_source: Path

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mcp-console-installation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prefix = self.root / "installation"
        (prefix / "bin").mkdir(parents=True)
        (prefix / "libexec").mkdir()
        self.binary = prefix / "bin" / "mcp-console"
        self.runner = prefix / "libexec" / "mcp-console-sandbox"
        shutil.copy2(self.binary_source, self.binary)
        shutil.copy2(self.runner_source, self.runner)
        if sys.platform == "linux":
            shutil.copy2(
                self.runner_source.with_name("bwrap"), self.runner.with_name("bwrap")
            )
        self.path = self.root / "path"
        self.path.mkdir()
        decoy = self.path / "mcp-console-sandbox"
        decoy.write_text(
            "#!/bin/sh\nprintf 'PATH runner was used\\n'\n", encoding="utf-8"
        )
        decoy.chmod(0o755)

    def run_sandbox(
        self, binary: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(binary or self.binary), "sandbox", "--", "/usr/bin/true"],
            input=b"",
            capture_output=True,
            cwd=self.root,
            env=os.environ | {"PATH": str(self.path)},
            timeout=30,
            check=False,
        )

    def test_runs_original_binary(self) -> None:
        result = self.run_sandbox(self.binary_source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_resolves_private_runner_through_public_symlink(self) -> None:
        link = self.root / "mcp-console"
        link.symlink_to(self.binary)
        result = self.run_sandbox(link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_setup_is_one_shot_with_idle_stdin(self) -> None:
        request = {
            "version": 2,
            "command": ["/bin/sh", "-c", "printf 'ready\\n'; exec /bin/cat"],
            "cwd": str(self.root.resolve()),
            "environment": {},
            "filesystem": {
                "kind": "restricted",
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }
                ],
            },
            "network": "restricted",
            "proxy": None,
        }
        payload = json.dumps(request).encode()
        read, write = os.pipe()
        relocated = fcntl.fcntl(read, fcntl.F_DUPFD_CLOEXEC, 73)
        os.close(read)
        with os.fdopen(relocated, "rb", buffering=0) as setup_read, os.fdopen(
            write, "wb"
        ) as setup_write:
            process = subprocess.Popen(
                [self.runner, "--bootstrap-fd", str(relocated)],
                pass_fds=(relocated,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            setup_read.close()
            try:
                setup_write.write(len(payload).to_bytes(4, "big") + payload)
                setup_write.flush()
                # Both setup and target-input writers remain open and idle.
                ready, _, _ = select.select([process.stdout], [], [], 10)
                self.assertTrue(ready, "setup waited for EOF or consumed target stdin")
                self.assertEqual(os.read(process.stdout.fileno(), 6), b"ready\n")
                with self.assertRaises(BrokenPipeError):
                    os.write(setup_write.fileno(), b"no setup reader may remain")
                sentinel = bytes(range(256))
                stdout, stderr = process.communicate(sentinel, timeout=10)
                self.assertEqual(
                    (process.returncode, stdout, stderr), (0, sentinel, b"")
                )
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_missing_private_runner_does_not_use_path(self) -> None:
        self.runner.unlink()
        result = self.run_sandbox()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"private sandbox runner", result.stderr)

    def test_rejects_a_different_private_executable(self) -> None:
        self.runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        result = self.run_sandbox()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"private sandbox runner", result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    parser.add_argument("runner", type=Path)
    args = parser.parse_args()
    SandboxInstallationTests.binary_source = args.binary.resolve()
    SandboxInstallationTests.runner_source = args.runner.resolve()
    unittest.main(argv=[sys.argv[0]])
