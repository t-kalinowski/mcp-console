from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from support.macos import (
    capture_darwin_process_identity,
    signal_darwin_process,
)

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
import os
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
        cleaned = os.open(root / "child-cleanup-complete", os.O_WRONLY | os.O_NONBLOCK)
        os.write(cleaned, b"1")
        os.close(cleaned)
    return [{"runner": "released"}]


def test_failure_beside_hang(binary: Path) -> list[dict[str, str]]:
    with (binary.parents[2] / "failure-release").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
    return [{"runner": "deliberate mismatch"}]


def test_fails_before_cleanup(binary: Path) -> list[dict[str, str]]:
    try:
        raise AssertionError("original failure before cleanup")
    finally:
        test_hangs(binary)


if __name__ == "__main__":
    signal.pause()
""".lstrip()

# fmt: python
SELF_INTERRUPTING_SUITE = """
import os
import signal
from pathlib import Path


def test_interrupts_itself(binary: Path) -> list[dict[str, str]]:
    os.kill(os.getpid(), signal.SIGINT)
    raise AssertionError("case continued after its own SIGINT")
""".lstrip()

# fmt: python
GIL_HOLDING_SUITE = """
import ctypes
import os
from pathlib import Path


def test_holds_gil(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    (root / "gil-case-pid").write_text(str(os.getpid()), encoding="utf-8")
    library = ctypes.PyDLL(str(root / "gil-checkpoint.dylib"))
    wait_for_release = library.wait_for_probe_release
    wait_for_release.argtypes = (ctypes.c_int, ctypes.c_int)
    wait_for_release.restype = ctypes.c_int
    with (
        (root / "gil-started").open("wb", buffering=0) as started,
        (root / "gil-release").open("rb", buffering=0) as release,
    ):
        assert wait_for_release(started.fileno(), release.fileno()) == 0
    return [{"runner": "released"}]
""".lstrip()

# fmt: python
FORKING_SUITE = """
import os
import warnings
from pathlib import Path


def test_forks(binary: Path) -> list[dict[str, object]]:
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", DeprecationWarning)
        child = os.fork()
        if child == 0:
            os._exit(0)
        assert os.waitpid(child, 0) == (child, 0)
    return [{"runner": "forked", "warnings": [str(item.message) for item in recorded]}]
""".lstrip()

# fmt: python
GATED_SNAPSHOT_CHECK = """
ungated_check_recording = check_recording


def check_recording(*arguments: object, **keywords: object) -> object:
    root = Path(__file__).resolve().parents[2]
    try:
        with (root / "snapshot-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
        with (root / "snapshot-release").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
        return ungated_check_recording(*arguments, **keywords)
    finally:
        (root / "snapshot-check-cleaned").touch()
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
        for name in (
            "hang-started",
            "hang-release",
            "failure-release",
            "child-cleanup-complete",
        ):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        process = self.start_runner(*arguments)
        try:
            yield process, checkpoints[0], checkpoints[2]
        finally:
            # Cases have their own sessions. Release their fixture waits even
            # when the runner itself fails before it can interrupt them.
            try:
                for checkpoint in checkpoints[1:3]:
                    os.write(checkpoint, b"1")
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=10)
                ready, _, _ = select.select([checkpoints[3]], [], [], 5)
                self.assertTrue(ready, "released case did not finish child cleanup")
                self.assertEqual(os.read(checkpoints[3], 1), b"1")
            finally:
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
            self.assertIn(f"{selector}: failed", stderr)
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

    def test_failure_cancels_hanging_sibling_without_another_failure(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        failure = "client_server/server/test_tools::failure_beside_hang"
        with self.hanging_runner(
            "--timeout", "60", "--jobs", "2", selector, failure
        ) as (
            process,
            started,
            release_failure,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertIn(f"{selector}: cancelled", stdout + stderr)
            self.assertNotIn(f"{selector}: failed", stdout + stderr)
            self.assertNotIn("timed out", stderr)
            self.assertNotIn("multiple transcript cases failed", stderr)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_independent_case_interrupt_remains_a_failure(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + SELF_INTERRUPTING_SUITE, encoding="utf-8")
        selector = "client_server/server/test_tools::interrupts_itself"
        result = self.run_runner(selector)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{selector}: failed", result.stderr)
        self.assertIn("KeyboardInterrupt", result.stderr)
        self.assertNotIn(f"{selector}: cancelled", result.stdout + result.stderr)

    def test_cancelled_cleanup_preserves_the_original_failure(self) -> None:
        selector = "client_server/server/test_tools::fails_before_cleanup"
        failure = "client_server/server/test_tools::failure_beside_hang"
        with self.hanging_runner(
            "--timeout", "60", "--jobs", "2", selector, failure
        ) as (process, started, release_failure):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "failed case did not enter its cleanup")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertIn(f"{selector}: cancelled", stdout + stderr)
            self.assertIn("AssertionError: original failure before cleanup", stderr)
            self.assertIn("KeyboardInterrupt", stderr)
            self.assertNotIn("multiple transcript cases failed", stderr)

    def assert_signal_retires_case(self, number: signal.Signals) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "60", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(number)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(selector, stderr)
            self.assertIn("KeyboardInterrupt", stderr)
            if number != signal.SIGINT:
                self.assertIn(f"transcript runner received {number.name}", stderr)
            self.assertTrue((self.root / "child-cleaned").is_file())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_interrupt_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGINT)

    def test_termination_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGTERM)

    def test_hangup_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGHUP)

    def test_interrupt_during_final_snapshot_check_is_reported(self) -> None:
        snapshots = self.root / "tests" / "support" / "snapshots.py"
        with snapshots.open("a", encoding="utf-8") as source:
            source.write("\n" + GATED_SNAPSHOT_CHECK)
        checkpoints = []
        for name in ("snapshot-started", "snapshot-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        started, release = checkpoints
        process = self.start_runner("client_server/server/test_tools::selected")
        try:
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "final snapshot check did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn("KeyboardInterrupt", stderr)
            self.assertTrue((self.root / "snapshot-check-cleaned").is_file())
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

    def assert_signal_stops_blocked_snapshot_check(
        self, number: signal.Signals
    ) -> None:
        snapshots = self.root / "tests" / "support" / "snapshots.py"
        with snapshots.open("a", encoding="utf-8") as source:
            source.write("\n" + GATED_SNAPSHOT_CHECK)
        checkpoints = []
        for name in ("snapshot-started", "snapshot-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        checking, release = checkpoints
        try:
            with self.hanging_runner(
                "--timeout",
                "60",
                "--jobs",
                "2",
                "client_server/server/test_tools::selected",
                "client_server/server/test_tools::hangs",
            ) as (process, sibling_started, _):
                try:
                    for checkpoint in (checking, sibling_started):
                        ready, _, _ = select.select([checkpoint], [], [], 10)
                        self.assertTrue(
                            ready, "snapshot check and sibling did not start"
                        )
                        self.assertEqual(os.read(checkpoint, 1), b"1")
                    process.send_signal(number)
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.fail(
                            "signal did not stop the blocked snapshot check and sibling"
                        )
                    self.assertNotEqual(process.returncode, 0, stdout)
                    self.assertIn("KeyboardInterrupt", stderr)
                    if number != signal.SIGINT:
                        self.assertIn(
                            f"transcript runner received {number.name}", stderr
                        )
                    self.assertTrue((self.root / "snapshot-check-cleaned").is_file())
                    self.assertTrue((self.root / "child-cleaned").is_file())
                finally:
                    # Either case can reach its snapshot check during cleanup.
                    os.write(release, b"11")
        finally:
            for checkpoint in checkpoints:
                os.close(checkpoint)

    def test_interrupt_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGINT)

    def test_termination_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGTERM)

    def test_hangup_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGHUP)

    def test_runner_loss_retires_its_detached_case(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "60", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            cleaned = os.open(
                self.root / "child-cleanup-complete", os.O_RDONLY | os.O_NONBLOCK
            )
            try:
                ready, _, _ = select.select([started], [], [], 10)
                self.assertTrue(ready, "hanging case did not start")
                self.assertEqual(os.read(started, 1), b"1")
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                ready, _, _ = select.select([cleaned], [], [], 5)
                self.assertTrue(
                    ready, "detached case outlived its runner without cleanup"
                )
                self.assertTrue((self.root / "child-cleaned").is_file())
            finally:
                os.close(cleaned)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS process exit events")
    def test_runner_loss_retires_case_holding_the_gil(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + GIL_HOLDING_SUITE, encoding="utf-8")
        (self.snapshots / "holds_gil.yaml").write_text(
            "---\nrunner: released\n...\n", encoding="utf-8"
        )
        subprocess.run(
            [
                "cc",
                "-dynamiclib",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                self.root / "gil-checkpoint.dylib",
                ROOT / "tests" / "fixtures" / "native" / "python_probe_checkpoint.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        checkpoints = []
        for name in ("gil-started", "gil-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        started, release = checkpoints
        process = self.start_runner(
            "--timeout",
            "60",
            "--jobs",
            "1",
            "client_server/server/test_tools::holds_gil",
        )
        identity = None
        exits = select.kqueue()
        try:
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "case did not enter its native GIL-holding call")
            self.assertEqual(os.read(started, 1), b"1")
            pid = int((self.root / "gil-case-pid").read_text(encoding="utf-8"))
            identity = capture_darwin_process_identity(pid)
            watch = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            self.assertEqual(exits.control([watch], 0, 0), [])
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            observed = exits.control(None, 1, 20)
            self.assertTrue(
                observed, "GIL-holding case outlived its runner's cleanup deadline"
            )
            self.assertEqual(observed[0].ident, pid)
            self.assertTrue(observed[0].fflags & select.KQ_NOTE_EXIT)
        finally:
            os.write(release, b"1")
            if identity is not None:
                signal_darwin_process(identity, signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            exits.close()
            for checkpoint in checkpoints:
                os.close(checkpoint)

    @unittest.skipUnless(
        sys.version_info >= (3, 12), "requires Python fork diagnostics"
    )
    def test_case_can_fork_without_thread_safety_warnings(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + FORKING_SUITE, encoding="utf-8")
        (self.snapshots / "forks.yaml").write_text(
            "---\nrunner: forked\nwarnings: []\n...\n", encoding="utf-8"
        )
        result = self.run_runner("client_server/server/test_tools::forks")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_case_platform_selection(self) -> None:
        with self.suite.open("a") as suite:
            suite.write('\nCASE_PLATFORMS = {"selected": {"unsupported"}}\n')
        result = self.run_runner("client_server/server/test_tools")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("selected: skipped on", result.stdout)
        self.assertFalse((self.root / "selected.marker").exists())
        self.assertTrue((self.root / "unselected.marker").exists())

    def test_platform_recording_preserves_other_platform_snapshots(self) -> None:
        with self.suite.open("a") as suite:
            suite.write("""
import sys
from support.records import TranscriptWithCompanions

def test_selected(binary):
    return TranscriptWithCompanions([{"runner": sys.platform}], {}, platform=sys.platform)
""")
        other = "linux" if sys.platform == "darwin" else "darwin"
        peer = self.snapshots / f"selected.{other}.yaml"
        peer.write_text(f"---\nrunner: {other}\n...\n")
        result = self.run_runner("--update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(peer.is_file())
        self.assertTrue((self.snapshots / "selected.yaml").is_file())
        actual = self.snapshots / f"selected.{sys.platform}.yaml"
        self.assertIn(sys.platform, actual.read_text())
        result = self.run_runner("client_server/server/test_tools::selected")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_platform_initialization_reference_preserves_shared_transcripts(
        self,
    ) -> None:
        reference = "tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml"
        with self.suite.open("a") as suite:
            suite.write("""
from support.records import TranscriptWithCompanions
import sys

def test_initializes_and_lists_tools(binary):
    return TranscriptWithCompanions([{"runner": sys.platform}], {}, platform=sys.platform)

def test_selected(binary):
    return [{"runner": sys.platform}, {"runner": "selected"}]
""")
        platform_reference = (
            self.snapshots / f"initializes_and_lists_tools.{sys.platform}.yaml"
        )
        platform_reference.write_text(f"---\nrunner: {sys.platform}\n...\n")
        shared = self.snapshots / "selected.yaml"
        shared.write_text(f"--- !same-as {reference}\n---\nrunner: selected\n...\n")
        result = self.run_runner("client_server/server/test_tools::selected")
        self.assertEqual(result.returncode, 0, result.stderr)
        platform_reference.write_text("---\nrunner: different platform metadata\n...\n")
        result = self.run_runner("client_server/server/test_tools::selected")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from its snapshot", result.stderr)

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
