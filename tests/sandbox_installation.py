from __future__ import annotations

import argparse
import os
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
