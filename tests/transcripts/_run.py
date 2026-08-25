# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12"]
# ///

import argparse
import difflib
import json
import os
import runpy
import shutil
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from dataclasses import dataclass
from multiprocessing import Manager
from pathlib import Path
from queue import Empty
from typing import Protocol

from _support import Transcript, TranscriptWithCompanion, YamlStream
from yaml12 import Yaml, format_yaml, parse_yaml, read_yaml

directory = Path(__file__).resolve().parent
root = directory.parents[1]
binary = root / "target" / "debug" / "mcp-console"
boundaries = {"client_server", "server_relay", "relay_worker", "cli"}
suite_paths = sorted(directory.rglob("[!_]*.py"))
initialization_suite = "client_server/server"
initialization_case = "initializes_and_lists_tools"
initialization_reference = (
    f"tests/transcripts/golden/{initialization_suite}/{initialization_case}.yaml"
)
SLOW_TEST_SECONDS = 5.0
FREQUENT_STATUS_SECONDS = 30.0
FREQUENT_STATUS_UNTIL_SECONDS = 180.0
LATER_STATUS_SECONDS = 60.0

parser = argparse.ArgumentParser(prog="scripts/test")
parser.add_argument("--list", action="store_true", dest="list_tests")
parser.add_argument("--update", action="store_true")
parser.add_argument(
    "-j",
    "--jobs",
    type=int,
    default=max(2, os.cpu_count() or 2),
    help="number of transcript cases to run concurrently (default: at least 2)",
)
parser.add_argument("selectors", nargs="*", metavar="BOUNDARY/SUITE[::CASE]")

TranscriptCase = Callable[[Path], Transcript | TranscriptWithCompanion]
RecordedTranscript = Transcript | TranscriptWithCompanion


class ProgressQueue(Protocol):
    def put(self, item: tuple[int, float]) -> None: ...

    def get_nowait(self) -> tuple[int, float]: ...


def suite_identifier(suite_path: Path) -> str:
    relative = suite_path.relative_to(directory).with_suffix("")
    assert len(relative.parts) >= 2 and relative.parts[0] in boundaries, (
        f"{suite_path.relative_to(root)} is not under a transcript boundary"
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


def identical(left: object, right: object) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            identical(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            identical(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return left == right


def iter_strings(value: object) -> Iterator[str]:
    if isinstance(value, Yaml):
        yield from iter_strings(value.value)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(key)
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)
    elif isinstance(value, str):
        yield value


def format_transcript(value: YamlStream) -> str:
    strings = tuple(iter_strings(value))
    prefix = "__MCP_CONSOLE_WHITESPACE_SCALAR_"
    while any(prefix in string for string in strings):
        prefix = f"_{prefix}"

    replacements: dict[str, str] = {}

    def protect(item: object) -> object:
        if isinstance(item, Yaml):
            return Yaml(protect(item.value), tag=item.tag)
        if isinstance(item, dict):
            return {protect(key): protect(mapped) for key, mapped in item.items()}
        if isinstance(item, list):
            return [protect(value) for value in item]
        if isinstance(item, str) and item:
            lines = item.splitlines()
            first_nonempty = next((line for line in lines if line), "")
            needs_quotes = (
                item.isspace()
                or first_nonempty.startswith((" ", "\t"))
                or any(line.isspace() for line in lines)
            )
        else:
            needs_quotes = False
        if needs_quotes:
            placeholder = f"{prefix}{len(replacements)}__"
            replacements[placeholder] = item
            return placeholder
        return item

    formatted = format_yaml(protect(value), multi=True)
    for placeholder, original in replacements.items():
        assert formatted.count(placeholder) == 1, placeholder
        formatted = formatted.replace(placeholder, json.dumps(original))
    formatted = "\n".join(
        "" if line.isspace() else line for line in formatted.split("\n")
    )
    assert identical(value, parse_yaml(formatted, multi=True)), (
        "formatted transcript did not round-trip"
    )
    return formatted


def check_golden(golden: Path, actual: YamlStream, case: str, *, update: bool) -> None:
    actual_text = format_transcript(actual)

    if update:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual_text, encoding="utf-8")
        print(f"updated {golden.relative_to(root)}", flush=True)
        return
    if not golden.exists():
        raise SystemExit(
            f"{golden.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = read_yaml(golden, multi=True)
    if not identical(actual, expected):
        expected_text = format_transcript(expected)
        difference = "".join(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                actual_text.splitlines(keepends=True),
                fromfile=str(golden.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{difference}{case} differs from its golden snapshot")


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


def without_request_ids(transcript: Transcript) -> Transcript:
    rendered = []
    for entry in transcript:
        entry = entry.copy()
        if entry.keys() & {"input", "send"}:
            entry.pop("id", None)
        rendered.append(entry)
    return rendered


def check_recording(
    suite_name: str,
    case_name: str,
    recorded: RecordedTranscript,
    *,
    update: bool,
) -> set[Path]:
    golden = directory / "golden" / suite_name / f"{case_name}.yaml"
    case = f"{suite_name}::{case_name}"
    if isinstance(recorded, TranscriptWithCompanion):
        actual = without_request_ids(recorded.transcript)
        companion = (
            golden.with_suffix(f".{recorded.companion_name}.yaml"),
            recorded.companion,
        )
    else:
        actual = without_request_ids(recorded)
        companion = None
    if golden != root / initialization_reference:
        reference = without_request_ids(
            read_yaml(root / initialization_reference, multi=True)
        )
        assert reference, f"{initialization_reference} contains no documents"
        if identical(actual[: len(reference)], reference):
            actual = [
                Yaml(initialization_reference, tag="!same-as"),
                *actual[len(reference) :],
            ]
    check_golden(golden, actual, case, update=update)
    checked = {golden}
    if companion is not None:
        check_golden(*companion, case, update=update)
        checked.add(companion[0])
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
    return (int(elapsed // LATER_STATUS_SECONDS) + 1) * LATER_STATUS_SECONDS


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


def prune_stale_goldens(suites: dict[str, Path], checked_goldens: set[Path]) -> None:
    golden_root = directory / "golden"
    checked_suites = {
        golden.parent.relative_to(golden_root).as_posix() for golden in checked_goldens
    }
    cases_by_suite: dict[str, tuple[str, ...]] = {}

    for golden in golden_root.rglob("*.yaml"):
        suite_name = golden.parent.relative_to(golden_root).as_posix()
        if suite_name not in suites:
            stale = True
        elif suite_name in checked_suites:
            stale = golden not in checked_goldens
        else:
            if suite_name not in cases_by_suite:
                cases, _, _ = load_suite(suites[suite_name])
                cases_by_suite[suite_name] = tuple(
                    f"{case_name}." for case_name in cases
                )
            stale = not golden.name.startswith(cases_by_suite[suite_name])

        if stale:
            golden.unlink()
            print(f"removed {golden.relative_to(root)}", flush=True)

    for path in sorted(golden_root.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def run_cases(
    selected: list[tuple[str, str, Path]],
    *,
    jobs: int,
    update: bool,
    checked_goldens: set[Path],
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
                        checked_goldens.update(checked)
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

    assert binary.is_file(), f"{binary.relative_to(root)} is missing; run scripts/test"
    assert suite_paths, "no transcript suites found"

    suites = {suite_identifier(path): path for path in suite_paths}
    if options.list_tests:
        for suite_name, suite_path in suites.items():
            cases, _, _ = load_suite(suite_path)
            for case_name in cases:
                print(f"{suite_name}::{case_name}")
        return

    selected = selected_cases(suites, options.selectors)
    checked_goldens: set[Path] = set()
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
            checked_goldens=checked_goldens,
            reporter=reporter,
        )
        run_cases(
            selected,
            jobs=options.jobs,
            update=options.update,
            checked_goldens=checked_goldens,
            reporter=reporter,
        )
        if options.update and not options.selectors:
            prune_stale_goldens(suites, checked_goldens)
    finally:
        reporter.close()


if __name__ == "__main__":
    main()
