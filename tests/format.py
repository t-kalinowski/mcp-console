#!/usr/bin/env python3
"""Public command regressions for formatter reporting."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class FormatTests(unittest.TestCase):
    def test_reports_every_formatter_and_strict_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/format", scripts / "format")
            commands = root / "commands"
            commands.mkdir()
            for failure in (False, True):
                for name in ("ruff", "yamark", "cargo", "air"):
                    path = commands / name
                    status = 7 if failure and name == "yamark" else 0
                    path.write_text(
                        f"#!/bin/sh\necho {name} >> attempts\nexit {status}\n"
                    )
                    path.chmod(0o755)
                for arguments in ([], ["--strict"]):
                    with self.subTest(failure=failure, arguments=arguments):
                        (root / "attempts").write_text("")
                        result = subprocess.run(
                            [scripts / "format", *arguments],
                            cwd=root,
                            env=os.environ | {"PATH": str(commands)},
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(
                            result.returncode,
                            int(failure and bool(arguments)),
                            result.stdout + result.stderr,
                        )
                        self.assertEqual(
                            (root / "attempts").read_text().splitlines(),
                            ["ruff", "yamark", "cargo", "air"],
                        )
                        self.assertEqual(
                            result.stdout.splitlines(),
                            [
                                "ruff: ok",
                                "yamark: failed (7)" if failure else "yamark: ok",
                                "rustfmt: ok",
                                "air: ok",
                            ],
                        )


if __name__ == "__main__":
    unittest.main()
