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

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mcp-console-installation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prefix = self.root / "installation"
        (prefix / "bin").mkdir(parents=True)
        self.binary = prefix / "bin" / "mcp-console"
        shutil.copy2(self.binary_source, self.binary)
        self.home = self.root / "home"
        self.home.mkdir()
        self.path = self.root / "path"
        self.path.mkdir()
        decoy = self.path / "mcp-console-sandbox"
        decoy.write_text(
            "#!/bin/sh\nprintf 'PATH runner was used\\n'\n", encoding="utf-8"
        )
        decoy.chmod(0o755)
        self.environment = os.environ | {"PATH": str(self.path), "HOME": str(self.home)}

    @property
    def runner(self) -> Path:
        runners = list(
            self.home.glob("Library/Caches/mcp-console/sandbox/*/mcp-console-sandbox")
        )
        self.assertEqual(len(runners), 1)
        return runners[0]

    def run_sandbox(
        self, binary: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(binary or self.binary), "sandbox", "--", "/usr/bin/true"],
            input=b"",
            capture_output=True,
            cwd=self.root,
            env=self.environment,
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
        result = self.run_sandbox()
        self.assertEqual(result.returncode, 0, result.stderr)
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

    def test_runs_without_companion_files_or_path_lookup(self) -> None:
        result = self.run_sandbox()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )
        self.assertEqual(
            {path.name for path in self.runner.parent.iterdir()},
            {"mcp-console-sandbox", "LICENSE", "NOTICE"},
        )
        self.assertTrue((self.runner.parent / "LICENSE").read_text())
        self.assertTrue((self.runner.parent / "NOTICE").read_text())

    def test_recreates_a_removed_cache(self) -> None:
        result = self.run_sandbox()
        self.assertEqual(result.returncode, 0, result.stderr)
        original = self.runner.read_bytes()
        shutil.rmtree(self.runner.parent)
        result = self.run_sandbox()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )
        self.assertEqual(self.runner.read_bytes(), original)

    def test_concurrent_first_launches(self) -> None:
        processes = [
            subprocess.Popen(
                [str(self.binary), "sandbox", "--", "/usr/bin/true"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.environment,
            )
            for _ in range(4)
        ]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual((process.returncode, stdout, stderr), (0, b"", b""))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=30)
        self.assertEqual(len(list(self.runner.parent.iterdir())), 3)

    def test_sandbox_cannot_write_to_the_runner_cache(self) -> None:
        result = self.run_sandbox()
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = self.runner.parent / "modified"
        result = subprocess.run(
            [str(self.binary), "sandbox", "--", "/usr/bin/touch", str(marker)],
            capture_output=True,
            env=self.environment,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_rejects_a_different_private_executable(self) -> None:
        result = self.run_sandbox()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.runner.chmod(0o700)
        self.runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        result = self.run_sandbox()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"private sandbox runner", result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    SandboxInstallationTests.binary_source = args.binary.resolve()
    unittest.main(argv=[sys.argv[0]])
