from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, suppress
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

# fmt: python
HANGING_SUITE = """
import signal
import subprocess
import sys
from pathlib import Path


def test_hangs(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    child = subprocess.Popen([sys.executable, __file__])
    try:
        with (root / "hang-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
        with (root / "hang-release").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
    finally:
        child.kill()
        child.wait(timeout=5)
        (root / "child-cleaned").touch()
    return [{"runner": "released"}]


def test_failure_beside_hang(binary: Path) -> list[dict[str, str]]:
    with (binary.parents[2] / "failure-release").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
    return [{"runner": "deliberate mismatch"}]


if __name__ == "__main__":
    signal.pause()
""".lstrip()

# fmt: python
GATED_SNAPSHOT_CHECK = """
import signal


ungated_check_recording = check_recording


def check_recording(*arguments: object, **keywords: object) -> object:
    root = Path(__file__).resolve().parents[2]
    previous = signal.getsignal(signal.SIGINT)

    def acknowledge_interrupt(number: int, frame: object) -> None:
        previous(number, frame)
        with (root / "interrupt-received").open("wb", buffering=0) as received:
            assert received.write(b"1") == 1

    signal.signal(signal.SIGINT, acknowledge_interrupt)
    try:
        with (root / "snapshot-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
        with (root / "snapshot-release").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
        return ungated_check_recording(*arguments, **keywords)
    finally:
        signal.signal(signal.SIGINT, previous)
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
        for name in ("__init__.py", "cases.py", "records.py", "snapshots.py"):
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

    @contextmanager
    def hanging_runner(
        self, *arguments: str
    ) -> Iterator[tuple[subprocess.Popen[str], int, int]]:
        self.suite.write_text(PUBLIC_SUITE + HANGING_SUITE, encoding="utf-8")
        for name in ("hangs", "failure_beside_hang"):
            (self.snapshots / f"{name}.yaml").write_text(
                "---\nrunner: released\n...\n", encoding="utf-8"
            )
        checkpoints = []
        for name in ("hang-started", "hang-release", "failure-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        process = self.start_runner(*arguments)
        try:
            yield process, checkpoints[0], checkpoints[2]
        finally:
            # Cases have their own sessions. Release their fixture waits even
            # when the runner itself fails before it can interrupt them.
            for checkpoint in checkpoints[1:]:
                os.write(checkpoint, b"1")
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            for checkpoint in checkpoints:
                os.close(checkpoint)

    def test_case_deadline_stops_a_hanging_case(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "2", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(selector, stderr)
            self.assertIn("timed out", stderr)
            self.assertTrue((self.root / "child-cleaned").is_file())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_case_deadline_stops_hang_during_other_failure_cleanup(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        failure = "client_server/server/test_tools::failure_beside_hang"
        with self.hanging_runner(
            "--timeout", "2", "--jobs", "2", selector, failure
        ) as (
            process,
            started,
            release_failure,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            assert process.stderr is not None
            reported = ""
            deadline = time.monotonic() + 10
            while f"{failure}: failed" not in reported:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "snapshot failure was not reported")
                ready, _, _ = select.select([process.stderr], [], [], remaining)
                self.assertTrue(ready, "snapshot failure was not reported")
                data = os.read(process.stderr.fileno(), 4096)
                self.assertTrue(data, "runner exited before reporting snapshot failure")
                reported += data.decode()
            self.assertNotIn(
                "timed out", reported[: reported.index(f"{failure}: failed")]
            )
            stdout, stderr = process.communicate(timeout=10)
            stderr = reported + stderr
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertIn(selector, stderr)
            self.assertIn("timed out", stderr)
            self.assertIn("multiple transcript cases failed", stderr)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_interrupt_stops_hanging_case_and_runs_cleanup(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "60", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(selector, stderr)
            self.assertIn("KeyboardInterrupt", stderr)
            self.assertTrue((self.root / "child-cleaned").is_file())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_interrupt_during_final_snapshot_check_is_reported(self) -> None:
        snapshots = self.root / "tests" / "support" / "snapshots.py"
        with snapshots.open("a", encoding="utf-8") as source:
            source.write("\n" + GATED_SNAPSHOT_CHECK)
        checkpoints = []
        for name in ("snapshot-started", "interrupt-received", "snapshot-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        started, received, release = checkpoints
        process = self.start_runner("client_server/server/test_tools::selected")
        try:
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "final snapshot check did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(signal.SIGINT)
            ready, _, _ = select.select([received], [], [], 10)
            self.assertTrue(ready, "runner did not handle the interrupt")
            self.assertEqual(os.read(received, 1), b"1")
            self.assertEqual(os.write(release, b"1"), 1)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn("KeyboardInterrupt", stderr)
        finally:
            os.write(release, b"1")
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            for checkpoint in checkpoints:
                os.close(checkpoint)

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
