"""Install from unstaged sources, then exercise the relocated public executable."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform == "darwin", "the sandbox requires macOS")
class CargoInstallationTests(unittest.TestCase):
    def test_install_from_unstaged_sources_is_self_contained(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="mcp-console-cargo-install-"
        ) as temporary:
            directory = Path(temporary)
            source = directory / "source"
            environment = os.environ.copy()
            environment.pop("MCP_CONSOLE_SANDBOX_SOURCE", None)
            tracked = (
                subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
                .decode()
                .split("\0")
            )
            for name in filter(None, tracked):
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)
            result = subprocess.run(
                [
                    "cargo",
                    "install",
                    "--path",
                    ".",
                    "--root",
                    str(directory / "installation"),
                    "--target-dir",
                    str(ROOT / "target" / "cargo-install-test"),
                    "--jobs",
                    "1",
                ],
                cwd=source,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            environment |= {
                "UV_TOOL_DIR": str(directory / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(directory / "uv-bin"),
                "CARGO_TARGET_DIR": str(ROOT / "target" / "cargo-install-test"),
            }
            result = subprocess.run(
                ["uv", "tool", "install", "--reinstall", "."],
                cwd=source,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            binary = directory / "mcp-console"
            shutil.move(directory / "installation" / "bin" / "mcp-console", binary)
            shutil.rmtree(source)
            shutil.rmtree(directory / "installation")
            # Make every compiled-in build path unavailable during runtime checks.
            target = ROOT / "target" / "cargo-install-test"
            hidden = directory / "build-artifacts"
            target.rename(hidden)
            try:
                for installed in (binary, directory / "uv-bin" / "mcp-console"):
                    with self.subTest(installer=installed):
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(ROOT / "tests" / "sandbox_installation.py"),
                                str(installed),
                            ],
                            capture_output=True,
                            text=True,
                            timeout=180,
                        )
                        self.assertEqual(
                            result.returncode, 0, result.stdout + result.stderr
                        )
            finally:
                hidden.rename(target)


if __name__ == "__main__":
    unittest.main()
