"""Check source and wheel installations using one shared Cargo target directory."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "requires macOS or Linux")
class InstallationTests(unittest.TestCase):
    def test_uv_installs_a_relocatable_bundle_from_unstaged_sources(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-console-install-") as temporary:
            directory = Path(temporary)
            source = directory / "source"
            target = ROOT / "target"
            environment = os.environ.copy()
            environment.pop("MCP_CONSOLE_SANDBOX_SOURCE", None)
            environment |= {
                "CARGO_TARGET_DIR": str(target),
                "UV_TOOL_DIR": str(directory / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(directory / "uv-bin"),
            }
            tracked = (
                subprocess.check_output(
                    [
                        "git",
                        "ls-files",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                        "-z",
                    ],
                    cwd=ROOT,
                )
                .decode()
                .split("\0")
            )
            for name in filter(None, tracked):
                if not (ROOT / name).is_file():
                    continue
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)

            private_files = (
                "libexec/mcp-console-sandbox",
                "share/licenses/mcp-console/LICENSE",
                "share/licenses/mcp-console/NOTICE",
                *(
                    ("libexec/bwrap", "share/licenses/mcp-console/bubblewrap-COPYING")
                    if sys.platform == "linux"
                    else ()
                ),
            )

            def stage_stale_wheel_data() -> None:
                stale = (
                    "libexec/obsolete-runner",
                    "share/licenses/mcp-console/Codex-LICENSE",
                    "share/licenses/mcp-console/Codex-NOTICE",
                )
                if sys.platform == "linux":
                    stale += private_files
                for relative in stale:
                    destination = source / "wheel-data/data" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"stale macOS wheel data\n")

            if sys.platform == "linux":
                stage_stale_wheel_data()
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
            print(result.stdout, flush=True)
            for remove_companions in (True, False):
                # Check both missing companions and stale additions after Cargo
                # has cached a build with all current companions still present.
                if remove_companions:
                    for relative in ("libexec", "share"):
                        shutil.rmtree(source / "wheel-data/data" / relative)
                stage_stale_wheel_data()
                # Reuse the source and target directory for wheel construction.
                result = subprocess.run(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--config-setting",
                        "maturin.build-args=--compatibility pypi",
                        "--out-dir",
                        str(directory / "dist"),
                    ],
                    cwd=source,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                print(result.stdout, flush=True)
                wheels = list((directory / "dist").glob("*.whl"))
                self.assertEqual(len(wheels), 1)
                with zipfile.ZipFile(wheels[0]) as archive:
                    actual = sorted(
                        name.split(".data/data/", 1)[1]
                        for name in archive.namelist()
                        if ".data/data/" in name
                    )
                self.assertEqual(actual, sorted(private_files))
            result = subprocess.run(
                ["uv", "build", "--sdist", "--out-dir", str(directory / "dist")],
                cwd=source,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            archives = list((directory / "dist").glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0]) as archive:
                wheel_data = [
                    name.split("/", 1)[1]
                    for name in archive.getnames()
                    if "/wheel-data/" in name
                ]
            self.assertEqual(wheel_data, ["wheel-data/data/.gitignore"])
            bundle = directory / "relocated"
            (bundle / "bin").mkdir(parents=True)
            binary = bundle / "bin/mcp-console"
            shutil.copy2(target / "release/mcp-console", binary)
            for relative in ("libexec", "share/licenses/mcp-console"):
                shutil.copytree(target / relative, bundle / relative)
            shutil.rmtree(source)
            # Make every compiled-in build path unavailable during runtime checks.
            hidden = directory / "build-artifacts"
            target.rename(hidden)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "release.py"),
                        "smoke-wheel",
                        str(wheels[0]),
                        str(binary),
                    ],
                    cwd=ROOT,
                    env=environment
                    | {
                        "UV_TOOL_DIR": str(directory / "wheel-tools"),
                        "UV_TOOL_BIN_DIR": str(directory / "wheel-bin"),
                    },
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                print(result.stdout, flush=True)
                for installed in (
                    binary,
                    directory / "uv-bin" / "mcp-console",
                    directory / "wheel-bin" / "mcp-console",
                ):
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
