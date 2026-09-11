# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "py-yaml12>=0.2.0",
#     "anyio>=4.9",
#     "mcp==2.*,>=2.2.0",
#     "anthropic[mcp]>=1.4.0",
#     "chatlas[mcp]>=0.23.0",
#     "openai>=3.11.0",
#     "openai-agents>=0.22.2",
#     "openai-codex>=0.147.0",
# ]
# ///

import argparse
import math
import os
import pickle
import runpy
import signal
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue

directory = Path(__file__).resolve().parent
root = directory.parents[1]
sys.path.insert(0, str(root / "tests"))

from support.cases import (
    CaseCancelled,
    CaseProcess,
    run_case_subprocess,
    supervise_case,
)
from support.execution import Execution
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import missing_reasons
from support.snapshots import (
    check_recording,
    initialization_case,
    initialization_suite,
    snapshot_directory,
    snapshot_path,
)

binary = root / "target" / "release" / "mcp-console"
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
FAILURE_SETTLE_SECONDS = 2.0

parser = argparse.ArgumentParser(prog="scripts/test")
actions = parser.add_mutually_exclusive_group()
actions.add_argument("--list", action="store_true", dest="list_tests")
actions.add_argument("--locate", metavar="SELECTOR")
parser.add_argument("--update", action="store_true")
parser.add_argument(
    "--timeout",
    type=float,
    default=600.0,
    help="case deadline in seconds; increase for slow resolver workflows (default: 600)",
)
parser.add_argument("--record-case", nargs=3, help=argparse.SUPPRESS)
parser.add_argument("--supervise-case", nargs=4, help=argparse.SUPPRESS)
parser.add_argument(
    "-j",
    "--jobs",
    type=int,
    default=max(2, os.cpu_count() or 2),
    help="number of transcript cases to run concurrently (default: at least 2)",
)
parser.add_argument("selectors", nargs="*", metavar="BOUNDARY/SUITE[::CASE]")

RecordedTranscript = Transcript | TranscriptWithCompanions
TranscriptCase = Callable[..., RecordedTranscript]


def suite_identifier(suite_path: Path) -> str:
    relative = suite_path.relative_to(directory).with_suffix("")
    assert len(relative.parts) >= 2 and relative.parts[0] in boundaries, (
        f"{suite_path.relative_to(root)} is not under a test boundary"
    )
    return relative.as_posix()


def load_suite(
    suite_path: Path,
) -> dict[str, TranscriptCase]:
    namespace = runpy.run_path(str(suite_path))
    cases = {
        name.removeprefix("test_"): value
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    }
    assert cases, f"{suite_path.relative_to(root)} defines no test_ functions"
    assert "PLATFORMS" not in namespace and "REQUIRED_COMMANDS" not in namespace, (
        f"{suite_path.relative_to(root)}: declare requirements beside cases"
    )
    return cases


def available_executions(
    case: TranscriptCase, selector: str, *, report: bool = False
) -> list[Execution | None]:
    available = []
    for execution in getattr(case, "executions", (None,)):
        requirements = getattr(case, "requirements", ())
        if execution is not None:
            requirements += execution.requirements
        reason = missing_reasons(requirements)
        if reason:
            if report:
                suffix = f"[{execution.name}]" if execution is not None else ""
                print(f"{selector}{suffix}: skipped; {reason}", flush=True)
        else:
            available.append(execution)
    return available


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
            cases = load_suite(suites[suite_name])
            cases_by_suite[suite_name] = tuple(f"{case_name}." for case_name in cases)
        if not snapshot.name.startswith(cases_by_suite[suite_name]):
            orphans.append(snapshot)
    return orphans


def locate(suites: dict[str, Path], selector: str) -> None:
    suite_name, separator, case_name = selector.partition("::")
    if suite_name not in suites:
        parser.error(f"unknown transcript suite: {suite_name}")

    suite_path = suites[suite_name]
    cases = load_suite(suite_path)
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


def record_case(suite_path: Path, case_name: str, *, update: bool) -> set[Path]:
    os.environ["MCP_CONSOLE_TEST_PYTHON"] = sys.executable
    case = load_suite(suite_path)[case_name]
    suite_name = suite_identifier(suite_path)
    modes = available_executions(case, f"{suite_name}::{case_name}")
    assert modes, "selected case has no available execution"
    checked = set()
    initialization = (suite_name, case_name) == (
        initialization_suite,
        initialization_case,
    )
    for index, execution in enumerate(modes):
        try:
            recorded = case(binary) if execution is None else case(binary, execution)
            mode_snapshots = check_recording(
                suite_name,
                case_name,
                recorded,
                update=update and (index == 0 or initialization),
                execution=execution.name if execution is not None else None,
            )
            assert initialization or index == 0 or mode_snapshots == checked, (
                "execution modes produced different companion snapshots"
            )
            checked.update(mode_snapshots)
        except BaseException as error:
            if execution is not None:
                error.add_note(f"execution mode: {execution.name}")
            raise

    return checked


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

    def cancel(self, index: int, diagnostics: str) -> None:
        running = self.running.pop(index)
        self._line(f"{running.selector}: cancelled", error=True)
        if diagnostics:
            print(diagnostics, end="", file=sys.stderr, flush=True)

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
        cases = load_suite(suite_path)

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

        selected.extend(
            (suite_name, case_name, suite_path)
            for case_name in case_names
            if available_executions(
                cases[case_name], f"{suite_name}::{case_name}", report=True
            )
        )
    return selected


def prune_stale_snapshots(checked_snapshots: set[Path], orphans: list[Path]) -> None:
    snapshot_root = snapshot_directory
    checked_cases = {
        snapshot.with_suffix("")
        for snapshot in checked_snapshots
        if snapshot.suffix == ".yaml"
    }
    orphans = set(orphans)

    for snapshot in snapshot_root.rglob("*"):
        if not snapshot.is_file() or snapshot.suffix not in {".yaml", ".md", ".qmd"}:
            continue
        owner = snapshot.parent / snapshot.name.split(".", 1)[0]
        stale = snapshot in orphans or (
            owner in checked_cases and snapshot not in checked_snapshots
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
    timeout: float,
    update: bool,
    checked_snapshots: set[Path],
    reporter: ProgressReporter,
) -> None:
    if not selected:
        return

    events: SimpleQueue[tuple[int, float | None, CaseProcess | None]] = SimpleQueue()
    executor = ThreadPoolExecutor(max_workers=min(jobs, len(selected)))
    futures: dict[int, Future[set[Path]]] = {}
    active: dict[int, CaseProcess] = {}
    errors: list[BaseException] = []
    abort_deadline: float | None = None
    interrupted = False
    # SimpleQueue.put is reentrant: SIGINT can wake the loop without
    # interrupting submission or completion bookkeeping.
    previous_signals = {
        number: signal.signal(
            number, lambda received, _frame: events.put((-received, None, None))
        )
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }

    def interruption(index: int) -> BaseException:
        number = signal.Signals(-index)
        if number == signal.SIGINT:
            return KeyboardInterrupt()
        return RuntimeError(f"transcript runner received {number.name}")

    def fail(error: BaseException) -> None:
        nonlocal abort_deadline
        errors.append(error)
        if len(errors) == 1:
            abort_deadline = time.monotonic() + FAILURE_SETTLE_SECONDS
            for future in futures.values():
                future.cancel()

    try:
        for index, (_, case_name, suite_path) in enumerate(selected):
            future = executor.submit(
                run_case_subprocess,
                suite_path,
                case_name,
                timeout,
                events,
                index,
                update=update,
            )
            futures[index] = future
            future.add_done_callback(
                lambda _future, index=index: events.put((index, None, None))
            )

        pending = set(futures)
        while pending:
            try:
                now = time.monotonic()
                if abort_deadline is not None and now >= abort_deadline:
                    interrupted = True
                    abort_deadline = None
                    for case in active.values():
                        case.interrupt()

                reporter.report_due()
                deadlines = [
                    running.started_at + running.next_status_at
                    for running in reporter.running.values()
                ]
                if abort_deadline is not None:
                    deadlines.append(abort_deadline)
                wait_seconds = (
                    max(0.0, min(deadlines) - time.monotonic()) if deadlines else None
                )
                index, started_at, case = events.get(timeout=wait_seconds)
                if index < 0:
                    fail(interruption(index))
                    continue
                suite_name, case_name, _ = selected[index]
                if started_at is not None:
                    assert case is not None
                    active[index] = case
                    reporter.start(index, f"{suite_name}::{case_name}", started_at)
                    if interrupted:
                        case.interrupt()
                    continue

                future = futures[index]
                if future.cancelled():
                    pending.remove(index)
                    continue
                if not reporter.is_running(index):
                    # Process launch can fail before the start event is sent.
                    reporter.start(
                        index, f"{suite_name}::{case_name}", time.monotonic()
                    )
                try:
                    checked = future.result()
                except CaseCancelled as cancelled:
                    reporter.cancel(index, str(cancelled))
                except BaseException as error:
                    reporter.finish(index, succeeded=False)
                    fail(error)
                else:
                    checked_snapshots.update(checked)
                    reporter.finish(index, succeeded=True, count_progress=not errors)
                pending.remove(index)
                active.pop(index, None)
            except Empty:
                continue
    finally:
        executor.shutdown(cancel_futures=True)
        for number, handler in previous_signals.items():
            signal.signal(number, handler)

    # A signal can arrive while reporting the final completion. All controllers
    # have joined, so any remaining event was queued by our signal handler.
    while True:
        try:
            index, _, _ = events.get_nowait()
        except Empty:
            break
        assert index < 0, index
        errors.append(interruption(index))

    if len(errors) > 1:
        raise BaseExceptionGroup("multiple transcript cases failed", errors) from None
    if errors:
        raise errors[0]


def main() -> None:
    options = parser.parse_args()
    if options.supervise_case is not None:
        suite_path, case_name, output_path, owner = options.supervise_case
        status = supervise_case(
            Path(suite_path),
            case_name,
            Path(output_path),
            int(owner),
            update=options.update,
        )
        if status < 0:
            number = -status
            if number != signal.SIGKILL:
                signal.signal(number, signal.SIG_DFL)
            os.kill(os.getpid(), number)
        raise SystemExit(status)
    if options.record_case is not None:
        suite_path, case_name, output_path = options.record_case
        checked = record_case(Path(suite_path), case_name, update=options.update)
        with Path(output_path).open("wb") as output:
            pickle.dump(checked, output)
        return
    if options.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not math.isfinite(options.timeout) or options.timeout <= 0:
        parser.error("--timeout must be a positive finite number of seconds")
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
            cases = load_suite(suite_path)
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
            timeout=options.timeout,
            update=options.update,
            checked_snapshots=checked_snapshots,
            reporter=reporter,
        )
        run_cases(
            selected,
            jobs=options.jobs,
            timeout=options.timeout,
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
