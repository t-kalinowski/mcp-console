# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12"]
# ///

import argparse
import difflib
import runpy
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from _support import Transcript, TranscriptWithCompanion, YamlStream
from yaml12 import Yaml, format_yaml, read_yaml


directory = Path(__file__).resolve().parent
root = directory.parents[1]
binary = root / "target" / "debug" / "mcp-console"
suite_paths = sorted(directory.glob("[!_]*.py"))
initialization_references = (
    "tests/transcripts/golden/server/initializes_and_lists_tools.yaml",
    "tests/transcripts/golden/server/initializes_and_lists_tools_with_configured_python.yaml",
    "tests/transcripts/golden/server/initializes_and_lists_tools_with_custom_worker.yaml",
)

parser = argparse.ArgumentParser(prog="scripts/test")
parser.add_argument("--list", action="store_true", dest="list_tests")
parser.add_argument("--update", action="store_true")
parser.add_argument("selectors", nargs="*", metavar="SUITE[::CASE]")
options = parser.parse_args()

assert binary.is_file(), f"{binary.relative_to(root)} is missing; run scripts/test"
assert suite_paths, "no transcript suites found"

TranscriptCase = Callable[[Path], Transcript | TranscriptWithCompanion]


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


def check_golden(golden: Path, actual: YamlStream, case: str) -> None:
    actual_text = format_yaml(actual, multi=True)

    if options.update:
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
        expected_text = format_yaml(expected, multi=True)
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


suites = {path.stem: path for path in suite_paths}

if options.list_tests:
    for suite_name, suite_path in suites.items():
        cases, _, _ = load_suite(suite_path)
        for case_name in cases:
            print(f"{suite_name}::{case_name}")
    raise SystemExit

selected_suites: dict[str, list[str] | None] = {}
if options.selectors:
    for selector in options.selectors:
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

for suite_name, selected_case_names in selected_suites.items():
    suite_path = suites[suite_name]
    cases, platforms, required_commands = load_suite(suite_path)

    if selected_case_names is None:
        selected_cases = cases.items()
    else:
        unknown_cases = [name for name in selected_case_names if name not in cases]
        if unknown_cases:
            parser.error(
                f"unknown transcript case in {suite_name}: {', '.join(unknown_cases)}"
            )
        selected_cases = ((name, cases[name]) for name in selected_case_names)

    if platforms is not None and sys.platform not in platforms:
        print(f"{suite_name}: skipped on {sys.platform}")
        continue

    missing_commands = sorted(
        command for command in required_commands if shutil.which(command) is None
    )
    if missing_commands:
        print(f"{suite_name}: skipped; missing {', '.join(missing_commands)} on PATH")
        continue

    for case_name, record_transcript in selected_cases:
        golden = directory / "golden" / suite_name / f"{case_name}.yaml"
        case = f"{suite_name}::{case_name}"
        recorded = record_transcript(binary)
        if isinstance(recorded, TranscriptWithCompanion):
            actual = recorded.transcript
            companion = (
                golden.with_suffix(f".{recorded.companion_name}.yaml"),
                recorded.companion,
            )
        else:
            actual = recorded
            companion = None
        for initialization_reference in initialization_references:
            if golden == root / initialization_reference:
                continue
            reference = read_yaml(root / initialization_reference, multi=True)
            assert reference, f"{initialization_reference} contains no documents"
            if identical(actual[: len(reference)], reference):
                actual = [
                    Yaml(initialization_reference, tag="!same-as"),
                    *actual[len(reference) :],
                ]
                break
        check_golden(golden, actual, case)
        if companion is not None:
            check_golden(*companion, case)
