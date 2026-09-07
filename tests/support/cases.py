import os
import pickle
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Lock, Thread
from typing import BinaryIO

CASE_CLEANUP_SECONDS = 15


class CaseCancelled(Exception):
    """A case exited with SIGINT after the runner requested cleanup."""


@dataclass
class CaseProcess:
    process: subprocess.Popen
    ownership: int
    _interrupt_lock: Lock = field(default_factory=Lock)
    _interrupted: bool = False

    def interrupt(self) -> None:
        # A case deadline and a sibling failure can request cleanup together.
        # The second request must not interrupt the case's finally blocks.
        with self._interrupt_lock:
            if not self._interrupted:
                self._interrupted = True
                os.close(self.ownership)


def _captured_output(stream: BinaryIO) -> str:
    # A descendant may retain the log descriptor after its case exits. Read a
    # fixed file-size snapshot instead of waiting for that descendant's EOF.
    length = os.fstat(stream.fileno()).st_size
    return os.pread(stream.fileno(), length, 0).decode("utf-8", errors="replace")


def supervise_case(
    suite_path: Path, case_name: str, result: Path, ownership: int, *, update: bool
) -> int:
    """Keep owner loss and forced cleanup independent of the case interpreter."""
    os.set_inheritable(ownership, False)
    runner = Path(__file__).resolve().parents[1] / "boundaries" / "_run.py"
    command = [sys.executable, runner, "--record-case", suite_path, case_name, result]
    if update:
        command.append("--update")
    process = subprocess.Popen(command)
    events: SimpleQueue[int | BaseException | None] = SimpleQueue()

    def reap() -> None:
        try:
            events.put(process.wait())
        except BaseException as error:
            events.put(error)

    def watch_owner() -> None:
        try:
            assert os.read(ownership, 1) == b""
        except BaseException as error:
            events.put(error)
        else:
            events.put(None)
        finally:
            os.close(ownership)

    # These threads belong to the supervisor, never to the interpreter that
    # runs fixtures with fork/preexec_fn or may block in native code with the GIL.
    reaper = Thread(target=reap, daemon=True)
    reaper.start()
    Thread(target=watch_owner, daemon=True).start()
    event = events.get()
    if event is None:
        process.send_signal(signal.SIGINT)
        try:
            event = events.get(timeout=CASE_CLEANUP_SECONDS)
        except Empty:
            process.kill()
            try:
                event = events.get(timeout=5)
            except Empty:
                raise TimeoutError(f"{case_name} did not exit after SIGKILL") from None
    if isinstance(event, BaseException):
        process.kill()
        reaper.join(timeout=5)
        raise event
    assert event is not None
    reaper.join()
    return event


def run_case_subprocess(
    suite_path: Path,
    case_name: str,
    timeout: float,
    events: SimpleQueue,
    index: int,
    *,
    update: bool,
) -> set[Path]:
    runner = Path(__file__).resolve().parents[1] / "boundaries" / "_run.py"
    selector = f"{suite_path.relative_to(runner.parent).with_suffix('')}::{case_name}"
    with (
        tempfile.TemporaryDirectory(prefix="mcp-console-case-") as directory,
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        result = Path(directory) / "result.pickle"
        started_at = time.monotonic()
        ownership_reader, ownership_writer = os.pipe()
        command = [
            sys.executable,
            runner,
            "--supervise-case",
            suite_path,
            case_name,
            result,
            str(ownership_reader),
        ]
        if update:
            command.append("--update")
        try:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=(ownership_reader,),
                env={
                    **os.environ,
                    "MCP_CONSOLE_TEST_CASE_DEADLINE": str(started_at + timeout),
                },
            )
        except BaseException:
            os.close(ownership_writer)
            raise
        finally:
            os.close(ownership_reader)
        case = CaseProcess(process, ownership_writer)
        try:
            events.put((index, started_at, case))
            completed = Event()
            reap_error: BaseException | None = None

            def reap() -> None:
                nonlocal reap_error
                try:
                    process.wait()
                except BaseException as error:
                    reap_error = error
                finally:
                    completed.set()

            # POSIX Popen.wait(timeout=...) polls. One blocking reaper wakes the
            # case deadline while its supervisor owns cleanup and escalation.
            reaper = Thread(target=reap, daemon=True)
            reaper.start()
            timed_out = not completed.wait(
                max(0, timeout - (time.monotonic() - started_at))
            )
            if timed_out:
                case.interrupt()
                # The external supervisor owns the case's bounded escalation.
                completed.wait()
            reaper.join()
            if reap_error is not None:
                raise reap_error

            output = _captured_output(stdout)
            errors = _captured_output(stderr)
            if timed_out:
                raise TimeoutError(
                    f"{selector} timed out after {timeout:g} seconds\n{output}{errors}"
                )
            # This is an observation-order label, not signal provenance: an
            # independent SIGINT racing cleanup can have the same exit status.
            # Preserve every diagnostic; the initiating failure still fails the run.
            if case._interrupted and process.returncode == -signal.SIGINT:
                raise CaseCancelled(f"{output}{errors}")
            if process.returncode != 0:
                raise RuntimeError(
                    f"{selector} exited with status {process.returncode}\n{output}{errors}"
                )
            if output:
                print(output, end="", flush=True)
            if errors:
                print(errors, end="", file=sys.stderr, flush=True)
            with result.open("rb") as stream:
                return pickle.load(stream)
        finally:
            case.interrupt()
