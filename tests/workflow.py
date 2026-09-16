#!/usr/bin/env python3
"""Exercise checkout ownership and validation records through development commands."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "checkout"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "target").mkdir()
        for name in ("check", "test", "with-checkout"):
            source = ROOT / "scripts" / name
            shutil.copy2(source, self.root / "scripts" / name)
        for name in ("checkout_workflow.py", "build_backend.py"):
            source = ROOT / name
            shutil.copy2(source, self.root / name)
        self.environment = os.environ | {
            "XDG_CACHE_HOME": str(self.directory / "cache"),
            "MCP_CONSOLE_CHECK_SLOTS": "1",
        }
        self.environment.pop("MCP_CONSOLE_CHECKOUT_LOCKS", None)
        self.environment.pop("MCP_CONSOLE_VALIDATION_RUN", None)
        self.write_script(
            "scripts/stage-sandbox-runner",
            # fmt: python
            """
            import os
            import sys
            from pathlib import Path

            if os.environ.get("HOLD_STAGE"):
                Path("target").rename("hidden-target")
                print("stage ready", flush=True)
                assert sys.stdin.buffer.read(1) == b"1"
            print("stage complete", flush=True)
            """,
        )
        for name in ("scripts/check-core", "tests/install.py"):
            self.write_script(name, 'print("phase complete")')
        self.write_script(
            "maturin.py",
            # fmt: python
            """
            def build_wheel(*args):
                return "fixture.whl"


            build_editable = build_wheel
            build_sdist = build_wheel
            get_requires_for_build_editable = build_wheel
            get_requires_for_build_sdist = build_wheel
            get_requires_for_build_wheel = build_wheel
            prepare_metadata_for_build_editable = build_wheel
            prepare_metadata_for_build_wheel = build_wheel
            """,
        )
        self.write_script(
            "scripts/test",
            # fmt: python
            """
            print("transcripts complete")
            """,
        )
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-qm",
                "Fixture",
            ],
            cwd=self.root,
            check=True,
        )

    def write_script(self, name: str, source: str) -> None:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + code(source))
        path.chmod(0o755)

    def run_command(
        self, *command: str, root: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=root or self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def start_check(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [self.root / "scripts/check"],
            cwd=self.root,
            env=self.environment | {"HOLD_STAGE": "1"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(self.finish_check, process)
        assert process.stdout is not None
        for line in process.stdout:
            if line.rstrip() == "stage ready":
                return process
        self.fail("check exited before the stage receipt")

    def finish_check(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.communicate("1", timeout=10)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()

    def records(self) -> list[dict]:
        return [
            json.loads(path.read_text())
            for path in (self.root / ".dev-workflow/runs").glob("*/result.json")
        ]

    def test_records_phase_failure_and_tested_revision(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            print("client_server/output/test_previews::example: failed", flush=True)
            raise SystemExit(7)
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        records = self.records()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["exit_status"], 7)
        self.assertEqual(
            record["revision"],
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=self.root, text=True
            ).strip(),
        )
        self.assertTrue(record["worktree_status"])
        self.assertEqual(
            [phase["name"] for phase in record["phases"]], ["stage", "core"]
        )
        self.assertEqual(
            record["failing_selectors"], ["client_server/output/test_previews::example"]
        )
        for phase in record["phases"]:
            self.assertGreaterEqual(phase["elapsed_seconds"], 0)
            self.assertTrue(Path(phase["log"]).is_file())
        self.assertIn("result.json", result.stdout)

    def test_signalled_phase_preserves_shell_exit_status(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import os
            import signal

            os.kill(os.getpid(), signal.SIGTERM)
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        (record,) = self.records()
        self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
        self.assertEqual(record["phases"][-1]["exit_status"], -signal.SIGTERM)

    def test_packaging_conflict_survives_target_rename(self) -> None:
        process = self.start_check()
        result = self.run_command(
            sys.executable, "-c", "import build_backend; build_backend.build_wheel('.')"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("checkout is busy", result.stdout + result.stderr)
        self.finish_check(process)
        result = self.run_command(
            sys.executable, "-c", "import build_backend; build_backend.build_wheel('.')"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_nested_packaging_reuses_checkout_owner(self) -> None:
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import subprocess
            import sys

            subprocess.run(
                [
                    "scripts/with-checkout",
                    sys.executable,
                    "-c",
                    "import build_backend; assert build_backend.build_wheel('.') == 'fixture.whl'",
                ],
                check=True,
            )
            """,
        )
        result = self.run_command("scripts/check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.records())
        self.assertTrue(all(record["exit_status"] == 0 for record in self.records()))

    def test_termination_records_status_and_releases_ownership(self) -> None:
        process = self.start_check()
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        (record,) = self.records()
        self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
        self.assertEqual(record["phases"][-1]["exit_status"], -signal.SIGTERM)
        result = self.run_command(
            sys.executable, "-c", "import build_backend; build_backend.build_wheel('.')"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_gate_budget_is_shared_across_checkouts(self) -> None:
        other = self.directory / "other"
        shutil.copytree(self.root, other)
        process = self.start_check()
        result = self.run_command("scripts/check", root=other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("full-check budget is busy", result.stdout + result.stderr)
        self.environment["MCP_CONSOLE_CHECK_SLOTS"] = "2"
        result = self.run_command("scripts/check", root=other)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.finish_check(process)


if __name__ == "__main__":
    unittest.main()
