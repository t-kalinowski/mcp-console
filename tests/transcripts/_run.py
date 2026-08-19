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
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from _support import Transcript, TranscriptWithCompanion, YamlStream
from yaml12 import Yaml, format_yaml, parse_yaml, read_yaml

directory = Path(__file__).resolve().parent
root = directory.parents[1]
binary = root / "target" / "debug" / "mcp-console"
suite_paths = sorted(directory.glob("[!_]*.py"))
initialization_suite = "server"
initialization_case = "initializes_and_lists_tools"
initialization_reference = (
    f"tests/transcripts/golden/{initialization_suite}/{initialization_case}.yaml"
)

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
parser.add_argument("selectors", nargs="*", metavar="SUITE[::CASE]")

TranscriptCase = Callable[[Path], Transcript | TranscriptWithCompanion]
RecordedTranscript = Transcript | TranscriptWithCompanion


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


def format_transcript_yaml(value: YamlStream) -> str:
    escaped: list[tuple[str, str]] = []

    def marker(replacement: str) -> str:
        value = f"__MCP_CONSOLE_ESCAPED_WHITESPACE_{len(escaped):04d}__"
        escaped.append((value, replacement))
        return value

    def protect_string(value: str) -> str:
        lines = value.splitlines()
        first_nonempty = next((line for line in lines if line), "")
        if value and (
            not value.strip()
            or first_nonempty.startswith((" ", "\t"))
            or any(
                (line and not line.strip()) or line.endswith((" ", "\t"))
                for line in lines
            )
        ):
            return marker(json.dumps(value, ensure_ascii=False))
        return value

    def protect(node: object) -> object:
        if isinstance(node, str):
            return protect_string(node)
        if isinstance(node, Yaml):
            return Yaml(protect(node.value), tag=node.tag)
        if isinstance(node, list):
            return [protect(item) for item in node]
        if isinstance(node, dict):
            return {protect(key): protect(item) for key, item in node.items()}
        return node

    protected = protect(value)
    text = format_yaml(protected, multi=True)
    for marker_value, replacement in escaped:
        assert text.count(marker_value) == 1, (
            f"escaped YAML marker is not unique: {marker_value}"
        )
        text = text.replace(marker_value, replacement)
    text = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
    assert identical(parse_yaml(text, multi=True), value), (
        "formatted transcript YAML did not round-trip"
    )
    return text


def check_golden(golden: Path, actual: YamlStream, case: str, *, update: bool) -> None:
    actual_text = format_transcript_yaml(actual)

    if update:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual_text, encoding="utf-8")
        print(f"updated {golden.relative_to(root)}")
        return
    if not golden.exists():
        raise SystemExit(
            f"{golden.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = read_yaml(golden, multi=True)
    if not identical(actual, expected):
        expected_text = format_transcript_yaml(expected)
        sys.stderr.writelines(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                actual_text.splitlines(keepends=True),
                fromfile=str(golden.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{case} differs from its golden snapshot")

    print(f"{golden.relative_to(root)}: ok")


def record_case(suite_path: Path, case_name: str) -> RecordedTranscript:
    cases, _, _ = load_suite(suite_path)
    return cases[case_name](binary)


def check_recording(
    suite_name: str,
    case_name: str,
    recorded: RecordedTranscript,
    *,
    update: bool,
) -> None:
    golden = directory / "golden" / suite_name / f"{case_name}.yaml"
    case = f"{suite_name}::{case_name}"
    if isinstance(recorded, TranscriptWithCompanion):
        actual = recorded.transcript
        companion = (
            golden.with_suffix(f".{recorded.companion_name}.yaml"),
            recorded.companion,
        )
    else:
        actual = recorded
        companion = None
    if golden != root / initialization_reference:
        reference = read_yaml(root / initialization_reference, multi=True)
        assert reference, f"{initialization_reference} contains no documents"
        if identical(actual[: len(reference)], reference):
            actual = [
                Yaml(initialization_reference, tag="!same-as"),
                *actual[len(reference) :],
            ]
    check_golden(golden, actual, case, update=update)
    if companion is not None:
        check_golden(*companion, case, update=update)


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


def main() -> None:
    options = parser.parse_args()
    if options.jobs < 1:
        parser.error("--jobs must be at least 1")

    assert binary.is_file(), f"{binary.relative_to(root)} is missing; run scripts/test"
    assert suite_paths, "no transcript suites found"

    suites = {path.stem: path for path in suite_paths}
    if options.list_tests:
        for suite_name, suite_path in suites.items():
            cases, _, _ = load_suite(suite_path)
            for case_name in cases:
                print(f"{suite_name}::{case_name}")
        return

    selected = selected_cases(suites, options.selectors)
    for index, (suite_name, case_name, suite_path) in enumerate(selected):
        if (suite_name, case_name) != (initialization_suite, initialization_case):
            continue
        recorded = record_case(suite_path, case_name)
        check_recording(suite_name, case_name, recorded, update=options.update)
        selected.pop(index)
        break

    arguments = [(suite_path, case_name) for _, case_name, suite_path in selected]
    if options.jobs == 1 or len(arguments) < 2:
        recordings = (record_case(*pair) for pair in arguments)
        for (suite_name, case_name, _), recorded in zip(selected, recordings):
            check_recording(suite_name, case_name, recorded, update=options.update)
        return

    max_workers = min(options.jobs, len(arguments))
    if sys.platform == "win32":
        max_workers = min(max_workers, 61)

    executor = ProcessPoolExecutor(max_workers=max_workers)
    try:
        futures = {
            executor.submit(record_case, *argument): index
            for index, argument in enumerate(arguments)
        }
        for future in as_completed(futures):
            suite_name, case_name, _ = selected[futures[future]]
            check_recording(
                suite_name,
                case_name,
                future.result(),
                update=options.update,
            )
    except BaseException:
        executor.shutdown(cancel_futures=True)
        raise
    else:
        executor.shutdown()


if __name__ == "__main__":
    main()
