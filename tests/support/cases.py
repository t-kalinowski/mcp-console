import os
import pickle
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import SimpleQueue
from threading import Event, Lock, Thread
from typing import BinaryIO

from support.records import Transcript, TranscriptWithCompanions

CASE_CLEANUP_SECONDS = 15


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


def record_case_subprocess(
    suite_path: Path,
    case_name: str,
    timeout: float,
    events: SimpleQueue,
    index: int,
) -> Transcript | TranscriptWithCompanions:
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
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    runner,
                    "--record-case",
                    suite_path,
                    case_name,
                    result,
                    str(ownership_reader),
                ],
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                pass_fds=(ownership_reader,),
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

            # POSIX Popen.wait(timeout=...) polls. One blocking reaper wakes each
            # deadline wait, and cannot hold the runner open if SIGKILL also fails.
            reaper = Thread(target=reap, daemon=True)
            reaper.start()
            timed_out = not completed.wait(
                max(0, timeout - (time.monotonic() - started_at))
            )
            if timed_out:
                case.interrupt()
                if not completed.wait(CASE_CLEANUP_SECONDS):
                    process.kill()
                    if not completed.wait(5):
                        raise TimeoutError(f"{selector} did not exit after SIGKILL")
            reaper.join()
            if reap_error is not None:
                raise reap_error

            output = _captured_output(stdout)
            errors = _captured_output(stderr)
            if timed_out:
                raise TimeoutError(
                    f"{selector} timed out after {timeout:g} seconds\n{output}{errors}"
                )
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
