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

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
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
        self.environment.pop("MCP_CONSOLE_VALIDATION_GROUP", None)
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

    def start_check(self, root: Path | None = None) -> subprocess.Popen[str]:
        root = root or self.root
        process = subprocess.Popen(
            [root / "scripts/check"],
            cwd=root,
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
        self.assertIn("result.json", result.stderr)

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
        for method in ("build_wheel", "build_sdist"):
            with self.subTest(method=method):
                result = self.run_command(
                    sys.executable,
                    "-c",
                    f"import build_backend; build_backend.{method}('.')",
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

    def test_cancellation_escalates_for_direct_and_nested_stubborn_phases(self) -> None:
        self.write_script(
            "stubborn.py",
            # fmt: python
            """
            import os
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print(f"stubborn ready {os.getpgrp()}", flush=True)
            signal.pause()
            """,
        )
        for nested in (False, True):
            with self.subTest(nested=nested):
                command = ["scripts/with-checkout"]
                if nested:
                    command.append("scripts/with-checkout")
                command += [sys.executable, "stubborn.py"]
                process = subprocess.Popen(
                    command,
                    cwd=self.root,
                    env=self.environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                )
                group = None
                try:
                    assert process.stdout is not None
                    receipt = read_lines(
                        process.stdout, 3 if nested else 2, "stubborn phase setup"
                    )
                    self.assertTrue(receipt[-1].startswith("stubborn ready "), receipt)
                    group = int(receipt[-1].rsplit(" ", 1)[1])
                    process.terminate()
                    self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
                    # EOF also proves the stubborn writer has retired.
                    process.communicate(timeout=10)
                    record = next(
                        r
                        for r in self.records()
                        if r["command"] == ["run", *command[1:]]
                    )
                    self.assertEqual(record["exit_status"], 128 + signal.SIGTERM)
                    if not nested:
                        self.assertEqual(
                            record["phases"][0]["exit_status"], -signal.SIGKILL
                        )
                finally:
                    if group is not None:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
                result = self.run_command(
                    "scripts/with-checkout", sys.executable, "-c", "pass"
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

    def test_nested_check_reuses_later_slot_after_first_slot_is_released(self) -> None:
        second, third = (self.directory / name for name in ("second", "third"))
        for root in (second, third):
            shutil.copytree(self.root, root)
        self.environment["MCP_CONSOLE_CHECK_SLOTS"] = "2"
        ready = FifoCheckpoint.create(self.directory / "nested-ready")
        self.addCleanup(ready.close)
        self.environment["NESTED_READY"] = str(ready.path)
        self.write_script(
            "scripts/check-core",
            # fmt: python
            """
            import os
            import subprocess
            import sys

            if os.environ.get("NESTED"):
                with open(os.environ["NESTED_READY"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")
                assert sys.stdin.buffer.read(1) == b"1"
            else:
                environment = os.environ | {"NESTED": "1"}
                environment.pop("HOLD_STAGE", None)
                subprocess.run(["scripts/check"], env=environment, check=True)
            """,
        )
        shutil.copy2(self.root / "scripts/check-core", second / "scripts/check-core")
        # The first holder and third caller use the ordinary, non-nesting fixture.
        shutil.copy2(third / "scripts/check-core", self.root / "scripts/check-core")
        first = self.start_check()
        nested = self.start_check(second)
        self.finish_check(first)
        assert nested.stdin is not None
        nested.stdin.write("1")
        nested.stdin.flush()
        ready.wait("nested check owns its inherited slot")
        result = self.run_command("scripts/check", root=third)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.finish_check(nested)

    def test_nested_launch_error_records_failure_after_successful_phase(self) -> None:
        shutil.copy2(ROOT / "scripts/check-core", self.root / "scripts/check-core")
        self.write_script("scripts/validate_runtime_sources.py", 'print("checked")')
        result = self.run_command("scripts/check")
        self.assertNotEqual(result.returncode, 0)
        record = next(r for r in self.records() if r["command"] == ["check-core"])
        self.assertEqual(record["phases"][0]["exit_status"], 0)
        self.assertEqual(record["phases"][1]["exit_status"], 1)
        self.assertEqual(record["exit_status"], 1)

    def test_wrapped_command_preserves_stdout_and_stderr(self) -> None:
        self.write_script(
            "output.py",
            # fmt: python
            """
            import sys

            print('{"answer": 42}')
            print("diagnostic", file=sys.stderr)
            """,
        )
        result = self.run_command("scripts/with-checkout", sys.executable, "output.py")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"answer": 42}\n')
        self.assertIn("diagnostic\n", result.stderr)
        self.assertIn("result.json", result.stderr)
        (record,) = self.records()
        output = Path(record["phases"][0]["log"]).read_text()
        self.assertIn('{"answer": 42}\n', output)
        self.assertIn("diagnostic\n", output)

    def test_repeated_cancellation_cannot_interrupt_escalation(self) -> None:
        self.write_script(
            "stubborn.py",
            # fmt: python
            """
            import os
            import signal


            def terminated(*_):
                with open(os.environ["TERM_RECEIPT"], "wb", buffering=0) as receipt:
                    receipt.write(b"1")


            signal.signal(signal.SIGTERM, terminated)
            print(f"stubborn ready {os.getpgrp()}", flush=True)
            while True:
                signal.pause()
            """,
        )
        receipt = FifoCheckpoint.create(self.directory / "term-received")
        self.addCleanup(receipt.close)
        process = subprocess.Popen(
            ["scripts/with-checkout", sys.executable, "stubborn.py"],
            cwd=self.root,
            env=self.environment | {"TERM_RECEIPT": str(receipt.path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        group = None
        try:
            assert process.stdout is not None
            output = read_lines(process.stdout, 2, "stubborn phase setup")
            group = int(output[-1].rsplit(" ", 1)[1])
            process.terminate()
            receipt.wait("cancellation reached the phase")
            process.send_signal(signal.SIGHUP)
            self.assertEqual(process.wait(timeout=10), 128 + signal.SIGTERM)
        finally:
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
