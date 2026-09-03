from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "boundaries" / "_run.py"

# fmt: python
PUBLIC_SUITE = """
from pathlib import Path


def record(binary: Path, name: str) -> list[dict[str, str]]:
    (binary.parents[2] / f"{name}.marker").touch()
    return [{"runner": name}]


def test_initializes_and_lists_tools(binary: Path) -> list[dict[str, str]]:
    return record(binary, "initialization")


def test_selected(binary: Path) -> list[dict[str, str]]:
    return record(binary, "selected")


def test_unselected(binary: Path) -> list[dict[str, str]]:
    return record(binary, "unselected")
""".lstrip()

# fmt: python
FAILING_SUITE = """
import os
from pathlib import Path


def fail_after_both_start(
    binary: Path,
    release_name: str,
    actual: str,
) -> list[dict[str, str]]:
    root = binary.parents[2]
    started = os.open(root / "started", os.O_WRONLY)
    try:
        assert os.write(started, b"1") == 1
    finally:
        os.close(started)
    release = os.open(root / release_name, os.O_RDONLY)
    try:
        assert os.read(release, 1)
    finally:
        os.close(release)
    return [{"runner": actual}]


def test_initializes_and_lists_tools(binary: Path) -> list[dict[str, str]]:
    return [{"runner": "initialization"}]


def test_first_failure(binary: Path) -> list[dict[str, str]]:
    return fail_after_both_start(binary, "release-first", "first actual")


def test_second_failure(binary: Path) -> list[dict[str, str]]:
    return fail_after_both_start(binary, "release-second", "second actual")
""".lstrip()


@unittest.skipUnless(os.name == "posix", "requires POSIX process and FIFO APIs")
class TranscriptRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.boundaries = self.root / "tests" / "boundaries"
        self.suite = self.boundaries / "client_server" / "server" / "test_tools.py"
        self.snapshots = (
            self.root
            / "tests"
            / "snapshots"
            / "client_server"
            / "server"
            / "test_tools"
        )
        support = self.root / "tests" / "support"
        binary = self.root / "target" / "debug" / "mcp-console"
        for directory in (self.suite.parent, self.snapshots, support, binary.parent):
            directory.mkdir(parents=True, exist_ok=True)

        shutil.copy2(RUNNER, self.boundaries / "_run.py")
        for name in ("__init__.py", "records.py", "snapshots.py"):
            shutil.copy2(ROOT / "tests" / "support" / name, support / name)
        self.suite.write_text(PUBLIC_SUITE, encoding="utf-8")
        binary.touch()
        for name in ("initializes_and_lists_tools", "selected", "unselected"):
            value = "initialization" if name == "initializes_and_lists_tools" else name
            (self.snapshots / f"{name}.yaml").write_text(
                f"---\nrunner: {value}\n...\n", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_runner(self, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            ["uv", "run", "--script", self.boundaries / "_run.py", *arguments],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def run_runner(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        process = self.start_runner(*arguments)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"runner did not exit; stdout={stdout!r}; stderr={stderr!r}")
        return subprocess.CompletedProcess(
            arguments, process.returncode, stdout, stderr
        )

    def test_collection_selectors_and_locate(self) -> None:
        hidden = (
            self.boundaries / "client_server" / "server" / "_private" / "test_hidden.py"
        )
        hidden.parent.mkdir()
        hidden.write_text(
            "def test_hidden(binary):\n    return [{'runner': 'hidden'}]\n",
            encoding="utf-8",
        )
        suite = "client_server/server/test_tools"
        cases = [
            f"{suite}::initializes_and_lists_tools",
            f"{suite}::selected",
            f"{suite}::unselected",
        ]

        listed = self.run_runner("--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.splitlines(), cases)

        located = self.run_runner("--locate", f"{suite}::selected")
        self.assertEqual(located.returncode, 0, located.stderr)
        self.assertEqual(located.stdout.splitlines()[0], f"{suite}::selected")
        self.assertIn(
            "source: tests/boundaries/client_server/server/test_tools.py:",
            located.stdout,
        )
        self.assertIn(
            "snapshot: tests/snapshots/client_server/server/test_tools/selected.yaml",
            located.stdout,
        )

        located_suite = self.run_runner("--locate", suite)
        self.assertEqual(located_suite.returncode, 0, located_suite.stderr)
        located_lines = located_suite.stdout.splitlines()
        self.assertEqual(len(located_lines), 3 * len(cases))
        for index, case in enumerate(cases):
            case_name = case.rsplit("::", 1)[1]
            self.assertEqual(located_lines[3 * index], case)
            self.assertRegex(
                located_lines[3 * index + 1],
                r"^  source: tests/boundaries/client_server/server/test_tools\.py:\d+$",
            )
            self.assertEqual(
                located_lines[3 * index + 2],
                "  snapshot: "
                f"tests/snapshots/client_server/server/test_tools/{case_name}.yaml",
            )

        selected = self.run_runner("--jobs", "1", f"{suite}::selected")
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertTrue((self.root / "selected.marker").is_file())
        self.assertFalse((self.root / "unselected.marker").exists())

        for marker in self.root.glob("*.marker"):
            marker.unlink()
        selected_suite = self.run_runner("--jobs", "1", suite)
        self.assertEqual(selected_suite.returncode, 0, selected_suite.stderr)
        self.assertEqual(
            {path.name for path in self.root.glob("*.marker")},
            {"initialization.marker", "selected.marker", "unselected.marker"},
        )

    def test_orphan_rejection_and_full_update_cleanup(self) -> None:
        orphan = self.snapshots / "deleted_case.yaml"
        orphan.write_text("---\nrunner: orphan\n...\n", encoding="utf-8")

        rejected = self.run_runner("--list")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "orphan snapshot: "
            "tests/snapshots/client_server/server/test_tools/deleted_case.yaml",
            rejected.stderr,
        )
        self.assertIn(
            "run scripts/test --update to remove orphan snapshots", rejected.stderr
        )

        updated = self.run_runner("--update", "--jobs", "2")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertFalse(orphan.exists())
        self.assertIn(
            "removed tests/snapshots/client_server/server/test_tools/deleted_case.yaml",
            updated.stdout,
        )
        self.assertEqual(
            {path.name for path in self.snapshots.iterdir()},
            {
                "initializes_and_lists_tools.yaml",
                "selected.yaml",
                "unselected.yaml",
            },
        )

    def test_parallel_failure_exits_and_reports_every_failure(self) -> None:
        self.suite.write_text(FAILING_SUITE, encoding="utf-8")
        for name in ("selected", "unselected"):
            (self.snapshots / f"{name}.yaml").unlink()
        for name in ("first_failure", "second_failure"):
            (self.snapshots / f"{name}.yaml").write_text(
                f"---\nrunner: {name} expected\n...\n",
                encoding="utf-8",
            )
        os.mkfifo(self.root / "started")
        os.mkfifo(self.root / "release-first")
        os.mkfifo(self.root / "release-second")
        started = os.open(self.root / "started", os.O_RDWR | os.O_NONBLOCK)
        release_first = os.open(self.root / "release-first", os.O_RDWR)
        release_second = os.open(self.root / "release-second", os.O_RDWR)
        process = self.start_runner("--jobs", "2")
        try:
            acknowledgements = b""
            while len(acknowledgements) < 2:
                ready, _, _ = select.select([started], [], [], 10)
                self.assertTrue(ready, "both failing cases did not start")
                acknowledgements += os.read(started, 2 - len(acknowledgements))
            self.assertEqual(os.write(release_first, b"1"), 1)
            assert process.stderr is not None
            expected_failure = "client_server/server/test_tools::first_failure: failed"
            observed_stderr = ""
            deadline = time.monotonic() + 10
            while expected_failure not in observed_stderr:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "first failure was not reported")
                ready, _, _ = select.select([process.stderr], [], [], remaining)
                self.assertTrue(ready, "first failure was not reported")
                line = process.stderr.readline()
                self.assertNotEqual(line, "", "runner exited before reporting failure")
                observed_stderr += line
            self.assertEqual(os.write(release_second, b"2"), 1)
            stdout, remaining_stderr = process.communicate(timeout=10)
            stderr = observed_stderr + remaining_stderr
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"runner did not exit; stdout={stdout!r}; stderr={stderr!r}")
        finally:
            os.close(started)
            os.close(release_first)
            os.close(release_second)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("client_server/server/test_tools::first_failure: failed", stderr)
        self.assertIn("client_server/server/test_tools::second_failure: failed", stderr)
        self.assertIn("runner: first actual", stderr)
        self.assertIn("runner: second actual", stderr)
        self.assertIn("multiple transcript cases failed (2 sub-exceptions)", stderr)
        self.assertIn(
            "client_server/server/test_tools::first_failure differs from its snapshot",
            stderr,
        )
        self.assertIn(
            "client_server/server/test_tools::second_failure differs from its snapshot",
            stderr,
        )
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)


if __name__ == "__main__":
    unittest.main()
