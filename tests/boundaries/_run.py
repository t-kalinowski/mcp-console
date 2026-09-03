# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12"]
# ///

import argparse
import os
import runpy
import shutil
import sys
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import Manager
from pathlib import Path
from queue import Empty
from typing import Protocol

directory = Path(__file__).resolve().parent
root = directory.parents[1]
sys.path.insert(0, str(root / "tests"))

from support.records import Transcript, TranscriptWithCompanions
from support.snapshots import (
    check_recording,
    initialization_case,
    initialization_suite,
    snapshot_directory,
    snapshot_path,
)

binary = root / "target" / "debug" / "mcp-console"
boundaries = {"client_server", "server_relay", "relay_worker", "cli"}
suite_paths = sorted(
    path
    for path in directory.rglob("*.py")
    if not any(part.startswith("_") for part in path.relative_to(directory).parts)
)
SLOW_TEST_SECONDS = 60.0
FREQUENT_STATUS_SECONDS = 120.0
FREQUENT_STATUS_UNTIL_SECONDS = 600.0
LATER_STATUS_SECONDS = 300.0

parser = argparse.ArgumentParser(prog="scripts/test")
actions = parser.add_mutually_exclusive_group()
actions.add_argument("--list", action="store_true", dest="list_tests")
actions.add_argument("--locate", metavar="SELECTOR")
parser.add_argument("--update", action="store_true")
parser.add_argument(
    "-j",
    "--jobs",
    type=int,
    default=max(2, os.cpu_count() or 2),
    help="number of transcript cases to run concurrently (default: at least 2)",
)
parser.add_argument("selectors", nargs="*", metavar="BOUNDARY/SUITE[::CASE]")

TranscriptCase = Callable[[Path], Transcript | TranscriptWithCompanions]
RecordedTranscript = Transcript | TranscriptWithCompanions


class ProgressQueue(Protocol):
    def put(self, item: tuple[int, float]) -> None: ...

    def get_nowait(self) -> tuple[int, float]: ...


def suite_identifier(suite_path: Path) -> str:
    relative = suite_path.relative_to(directory).with_suffix("")
    assert len(relative.parts) >= 2 and relative.parts[0] in boundaries, (
        f"{suite_path.relative_to(root)} is not under a test boundary"
    )
    return relative.as_posix()


def load_suite(
    suite_path: Path,
) -> tuple[dict[str, TranscriptCase], set[str] | None, set[str]]:
    namespace = runpy.run_path(str(suite_path))
    cases = {
        name.removeprefix("test_"): value
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    }
    assert cases, f"{suite_path.relative_to(root)} defines no test_ functions"
    platforms = namespace.get("PLATFORMS")
    assert platforms is None or isinstance(platforms, set), (
        f"{suite_path.relative_to(root)} PLATFORMS must be a set"
    )
    required_commands = namespace.get("REQUIRED_COMMANDS", set())
    assert isinstance(required_commands, set) and all(
        isinstance(command, str) and command for command in required_commands
    ), f"{suite_path.relative_to(root)} REQUIRED_COMMANDS must be a set of names"
    return cases, platforms, required_commands


def orphan_snapshots(suites: dict[str, Path]) -> list[Path]:
    cases_by_suite: dict[str, tuple[str, ...]] = {}
    orphans = []
    for snapshot in sorted(snapshot_directory.rglob("*")):
        if not snapshot.is_file() or snapshot.suffix not in {".yaml", ".md", ".qmd"}:
            continue
        suite_name = snapshot.parent.relative_to(snapshot_directory).as_posix()
        if suite_name not in suites:
            orphans.append(snapshot)
            continue
        if suite_name not in cases_by_suite:
            cases, _, _ = load_suite(suites[suite_name])
            cases_by_suite[suite_name] = tuple(f"{case_name}." for case_name in cases)
        if not snapshot.name.startswith(cases_by_suite[suite_name]):
            orphans.append(snapshot)
    return orphans


def locate(suites: dict[str, Path], selector: str) -> None:
    suite_name, separator, case_name = selector.partition("::")
    if suite_name not in suites:
        parser.error(f"unknown transcript suite: {suite_name}")

    suite_path = suites[suite_name]
    cases, _, _ = load_suite(suite_path)
    if separator:
        if case_name not in cases:
            parser.error(f"unknown transcript case in {suite_name}: {case_name}")
        case_names = [case_name]
    else:
        case_names = list(cases)

    source = suite_path.relative_to(root)
    for case_name in case_names:
        line = cases[case_name].__code__.co_firstlineno
        snapshot = snapshot_path(suite_name, case_name).relative_to(root)
        print(f"{suite_name}::{case_name}")
        print(f"  source: {source}:{line}")
        print(f"  snapshot: {snapshot}")


def record_case(
    suite_path: Path,
    case_name: str,
    progress: ProgressQueue | None = None,
    progress_id: int | None = None,
) -> RecordedTranscript:
    if progress is not None:
        assert progress_id is not None
        progress.put((progress_id, time.monotonic()))
    cases, _, _ = load_suite(suite_path)
    return cases[case_name](binary)


def format_duration(elapsed: float) -> str:
    seconds = max(0, int(elapsed))
    minutes, seconds = divmod(seconds, 60)
    if minutes == 0:
        return f"{seconds}s"
    if seconds == 0:
        return f"{minutes}m"
    return f"{minutes}m {seconds}s"


def next_status_after(elapsed: float) -> float:
    if elapsed < FREQUENT_STATUS_UNTIL_SECONDS:
        next_status = (int(elapsed // FREQUENT_STATUS_SECONDS) + 1) * (
            FREQUENT_STATUS_SECONDS
        )
        return max(FREQUENT_STATUS_SECONDS, next_status)
    later_elapsed = elapsed - FREQUENT_STATUS_UNTIL_SECONDS
    return (
        FREQUENT_STATUS_UNTIL_SECONDS
        + (int(later_elapsed // LATER_STATUS_SECONDS) + 1) * LATER_STATUS_SECONDS
    )


@dataclass
class RunningCase:
    selector: str
    started_at: float
    next_status_at: float = SLOW_TEST_SECONDS
    reported: bool = False


class ProgressReporter:
    def __init__(self, *, update: bool) -> None:
        self.update = update
        self.running: dict[int, RunningCase] = {}
        self.progress_line_open = False

    def start(self, index: int, selector: str, started_at: float) -> None:
        assert index not in self.running, index
        self.running[index] = RunningCase(selector, started_at)

    def is_running(self, index: int) -> bool:
        return index in self.running

    def report_due(self) -> None:
        now = time.monotonic()
        for running in self.running.values():
            elapsed = now - running.started_at
            if elapsed < running.next_status_at:
                continue
            self._line(f"{running.selector}: running for {format_duration(elapsed)}")
            running.reported = True
            running.next_status_at = next_status_after(elapsed)

    def finish(
        self,
        index: int,
        *,
        succeeded: bool,
        count_progress: bool = True,
    ) -> None:
        running = self.running.pop(index)
        elapsed = time.monotonic() - running.started_at
        if succeeded:
            if running.reported or elapsed >= SLOW_TEST_SECONDS:
                self._line(
                    f"{running.selector}: finished in {format_duration(elapsed)}"
                )
            if count_progress and not self.update:
                self._dot()
        elif running.reported or elapsed >= SLOW_TEST_SECONDS:
            self._line(
                f"{running.selector}: failed in {format_duration(elapsed)}",
                error=True,
            )
        else:
            self._line(f"{running.selector}: failed", error=True)

    def close(self) -> None:
        if self.progress_line_open:
            print(flush=True)
            self.progress_line_open = False

    def _dot(self) -> None:
        print(".", end="", flush=True)
        self.progress_line_open = True

    def _line(self, message: str, *, error: bool = False) -> None:
        self.close()
        print(message, file=sys.stderr if error else sys.stdout, flush=True)


def drain_started(
    progress: ProgressQueue,
    selected: list[tuple[str, str, Path]],
    reporter: ProgressReporter,
) -> None:
    while True:
        try:
            index, started_at = progress.get_nowait()
        except Empty:
            return
        assert 0 <= index < len(selected), index
        assert not reporter.is_running(index), index
        suite_name, case_name, _ = selected[index]
        reporter.start(
            index,
            f"{suite_name}::{case_name}",
            started_at,
        )


def selected_cases(
    suites: dict[str, Path], selectors: list[str]
) -> list[tuple[str, str, Path]]:
    selected_suites: dict[str, list[str] | None] = {}
    if selectors:
        for selector in selectors:
            suite_name, separator, case_name = selector.partition("::")
            if suite_name not in suites:
                parser.error(f"unknown transcript suite: {suite_name}")

            if not separator:
                selected_suites[suite_name] = None
            elif suite_name not in selected_suites:
                selected_suites[suite_name] = [case_name]
            elif selected_suites[suite_name] is not None:
                selected_suites[suite_name].append(case_name)
    else:
        selected_suites = dict.fromkeys(suites)

    selected: list[tuple[str, str, Path]] = []
    for suite_name, selected_case_names in selected_suites.items():
        suite_path = suites[suite_name]
        cases, platforms, required_commands = load_suite(suite_path)

        if selected_case_names is None:
            case_names = list(cases)
        else:
            unknown_cases = [name for name in selected_case_names if name not in cases]
            if unknown_cases:
                parser.error(
                    f"unknown transcript case in {suite_name}: "
                    f"{', '.join(unknown_cases)}"
                )
            case_names = selected_case_names

        if platforms is not None and sys.platform not in platforms:
            print(f"{suite_name}: skipped on {sys.platform}")
            continue

        missing_commands = sorted(
            command for command in required_commands if shutil.which(command) is None
        )
        if missing_commands:
            print(
                f"{suite_name}: skipped; missing {', '.join(missing_commands)} on PATH"
            )
            continue

        selected.extend((suite_name, case_name, suite_path) for case_name in case_names)
    return selected


def prune_stale_snapshots(checked_snapshots: set[Path], orphans: list[Path]) -> None:
    snapshot_root = snapshot_directory
    checked_suites = {
        snapshot.parent.relative_to(snapshot_root).as_posix()
        for snapshot in checked_snapshots
    }
    orphans = set(orphans)

    for snapshot in snapshot_root.rglob("*"):
        if not snapshot.is_file() or snapshot.suffix not in {".yaml", ".md", ".qmd"}:
            continue
        suite_name = snapshot.parent.relative_to(snapshot_root).as_posix()
        stale = snapshot in orphans or (
            suite_name in checked_suites and snapshot not in checked_snapshots
        )

        if stale:
            snapshot.unlink()
            print(f"removed {snapshot.relative_to(root)}", flush=True)

    for path in sorted(snapshot_root.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def run_cases(
    selected: list[tuple[str, str, Path]],
    *,
    jobs: int,
    update: bool,
    checked_snapshots: set[Path],
    reporter: ProgressReporter,
) -> None:
    if not selected:
        return

    max_workers = min(jobs, len(selected))
    if sys.platform == "win32":
        max_workers = min(max_workers, 61)

    with Manager() as manager:
        progress = manager.Queue()
        executor = ProcessPoolExecutor(max_workers=max_workers)
        futures: dict[Future[RecordedTranscript], int] = {}
        try:
            for index, (_, case_name, suite_path) in enumerate(selected):
                future = executor.submit(
                    record_case,
                    suite_path,
                    case_name,
                    progress,
                    index,
                )
                futures[future] = index

            pending = set(futures)
            while pending:
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                drain_started(progress, selected, reporter)
                for future in sorted(done, key=futures.__getitem__):
                    index = futures[future]
                    suite_name, case_name, _ = selected[index]
                    selector = f"{suite_name}::{case_name}"
                    assert reporter.is_running(index), (
                        f"{selector} completed before reporting that it started"
                    )
                    try:
                        recorded = future.result()
                        checked = check_recording(
                            suite_name,
                            case_name,
                            recorded,
                            update=update,
                        )
                    except BaseException:
                        reporter.finish(index, succeeded=False)
                        raise
                    else:
                        checked_snapshots.update(checked)
                        reporter.finish(index, succeeded=True)
                reporter.report_due()
        except BaseException:
            for future in futures:
                future.cancel()
            unfinished = set(futures)
            while unfinished:
                done, unfinished = wait(
                    unfinished,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                drain_started(progress, selected, reporter)
                for future in done:
                    index = futures[future]
                    if not reporter.is_running(index):
                        continue
                    try:
                        future.result()
                    except BaseException:
                        reporter.finish(index, succeeded=False)
                    else:
                        reporter.finish(
                            index,
                            succeeded=True,
                            count_progress=False,
                        )
                reporter.report_due()
            executor.shutdown(cancel_futures=True)
            drain_started(progress, selected, reporter)
            raise
        else:
            executor.shutdown()
            drain_started(progress, selected, reporter)


def main() -> None:
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error("--jobs must be at least 1")
    if options.locate is not None and options.update:
        parser.error("--locate cannot be combined with --update")

    assert binary.is_file(), f"{binary.relative_to(root)} is missing; run scripts/test"
    assert suite_paths, "no transcript suites found"

    suites = {suite_identifier(path): path for path in suite_paths}
    orphans = orphan_snapshots(suites)
    full_update = (
        options.update
        and not options.selectors
        and not options.list_tests
        and options.locate is None
    )
    if orphans and not full_update:
        for orphan in orphans:
            print(f"orphan snapshot: {orphan.relative_to(root)}", file=sys.stderr)
        raise SystemExit("run scripts/test --update to remove orphan snapshots")

    if options.list_tests:
        for suite_name, suite_path in suites.items():
            cases, _, _ = load_suite(suite_path)
            for case_name in cases:
                print(f"{suite_name}::{case_name}")
        return
    if options.locate is not None:
        if options.selectors:
            parser.error("--locate does not accept additional selectors")
        locate(suites, options.locate)
        return

    selected = selected_cases(suites, options.selectors)
    checked_snapshots: set[Path] = set()
    initialization: list[tuple[str, str, Path]] = []
    for index, (suite_name, case_name, _) in enumerate(selected):
        if (suite_name, case_name) != (initialization_suite, initialization_case):
            continue
        initialization.append(selected.pop(index))
        break

    reporter = ProgressReporter(update=options.update)
    try:
        run_cases(
            initialization,
            jobs=1,
            update=options.update,
            checked_snapshots=checked_snapshots,
            reporter=reporter,
        )
        run_cases(
            selected,
            jobs=options.jobs,
            update=options.update,
            checked_snapshots=checked_snapshots,
            reporter=reporter,
        )
        if options.update and not options.selectors:
            prune_stale_snapshots(checked_snapshots, orphans)
    finally:
        reporter.close()


if __name__ == "__main__":
    main()
