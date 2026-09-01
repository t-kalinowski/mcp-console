from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "transcripts" / "_run.py"
SUPPORT = ROOT / "tests" / "transcripts" / "_support.py"


def write_file(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")


class TranscriptRunnerTests(unittest.TestCase):
    def test_discovers_nested_suite_and_ignores_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            transcripts = temporary / "tests" / "transcripts"
            transcripts.mkdir(parents=True)
            shutil.copy2(RUNNER, transcripts / "_run.py")
            shutil.copy2(SUPPORT, transcripts / "_support.py")
            binary = temporary / "target" / "debug" / "mcp-console"
            binary.parent.mkdir(parents=True)
            binary.touch()

            write_file(
                transcripts / "client_server" / "nested" / "suite.py",
                """
                import os
                from pathlib import Path


                def test_is_discovered(binary: Path) -> list[dict[str, str]]:
                    Path(os.environ["NESTED_SUITE_EXECUTED"]).touch()
                    return [{"runner": "nested"}]
                """,
            )
            write_file(
                transcripts / "client_server" / "_private" / "suite.py",
                """
                import os
                from pathlib import Path


                Path(os.environ["PRIVATE_SUITE_LOADED"]).touch()


                def test_is_not_discovered(binary: Path) -> list[dict[str, str]]:
                    Path(os.environ["PRIVATE_SUITE_EXECUTED"]).touch()
                    return [{"runner": "private"}]
                """,
            )
            write_file(
                transcripts
                / "golden"
                / "client_server"
                / "server"
                / "initializes_and_lists_tools.yaml",
                """
                ---
                runner: initialized
                ...
                """,
            )
            write_file(
                transcripts
                / "golden"
                / "client_server"
                / "nested"
                / "suite"
                / "is_discovered.yaml",
                """
                ---
                runner: nested
                ...
                """,
            )

            nested_executed = temporary / "nested-executed"
            private_loaded = temporary / "private-loaded"
            private_executed = temporary / "private-executed"
            environment = os.environ.copy()
            environment.update(
                {
                    "NESTED_SUITE_EXECUTED": str(nested_executed),
                    "PRIVATE_SUITE_LOADED": str(private_loaded),
                    "PRIVATE_SUITE_EXECUTED": str(private_executed),
                }
            )

            listed = subprocess.run(
                ["uv", "run", "--script", str(transcripts / "_run.py"), "--list"],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(
                listed.stdout,
                "client_server/nested/suite::is_discovered\n",
            )
            self.assertFalse(private_loaded.exists())
            self.assertFalse(private_executed.exists())

            executed = subprocess.run(
                [
                    "uv",
                    "run",
                    "--script",
                    str(transcripts / "_run.py"),
                    "client_server/nested/suite::is_discovered",
                ],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertTrue(nested_executed.exists())
            self.assertFalse(private_loaded.exists())
            self.assertFalse(private_executed.exists())


if __name__ == "__main__":
    unittest.main()
