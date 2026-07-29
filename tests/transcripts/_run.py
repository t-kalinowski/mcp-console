# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12==0.1.0"]
# ///

import argparse
import difflib
import runpy
import sys
from collections.abc import Callable
from pathlib import Path

from _support import Transcript
from yaml12 import format_yaml


directory = Path(__file__).resolve().parent
root = directory.parents[1]
binary = root / "target" / "debug" / "mcp-console"
suite_paths = sorted(directory.glob("[!_]*.py"))

parser = argparse.ArgumentParser(prog="scripts/test")
parser.add_argument("--list", action="store_true", dest="list_tests")
parser.add_argument("--update", action="store_true")
parser.add_argument("selectors", nargs="*", metavar="SUITE[::CASE]")
options = parser.parse_args()

assert binary.is_file(), f"{binary.relative_to(root)} is missing; run scripts/test"
assert suite_paths, "no transcript suites found"

TranscriptCase = Callable[[Path], Transcript]


def load_cases(suite_path: Path) -> dict[str, TranscriptCase]:
    namespace = runpy.run_path(str(suite_path))
    cases = {
        name.removeprefix("test_"): value
        for name, value in namespace.items()
        if name.startswith("test_") and callable(value)
    }
    assert cases, f"{suite_path.relative_to(root)} defines no test_ functions"
    return cases


suites = {path.stem: path for path in suite_paths}

if options.list_tests:
    for suite_name, suite_path in suites.items():
        for case_name in load_cases(suite_path):
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
    cases = load_cases(suite_path)

    if selected_case_names is None:
        selected_cases = cases.items()
    else:
        unknown_cases = [name for name in selected_case_names if name not in cases]
        if unknown_cases:
            parser.error(
                f"unknown transcript case in {suite_name}: {', '.join(unknown_cases)}"
            )
        selected_cases = ((name, cases[name]) for name in selected_case_names)

    for case_name, record_transcript in selected_cases:
        transcript = format_yaml(record_transcript(binary), multi=True)
        golden = directory / "golden" / suite_name / f"{case_name}.yaml"

        if options.update:
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(transcript, encoding="utf-8")
            print(f"updated {golden.relative_to(root)}")
        elif not golden.exists():
            raise SystemExit(
                f"{golden.relative_to(root)} is missing; "
                f"run scripts/test --update {suite_name}::{case_name}"
            )
        else:
            expected = golden.read_text(encoding="utf-8")
            if transcript != expected:
                sys.stderr.writelines(
                    difflib.unified_diff(
                        expected.splitlines(keepends=True),
                        transcript.splitlines(keepends=True),
                        fromfile=str(golden.relative_to(root)),
                        tofile="actual",
                    )
                )
                raise SystemExit(
                    f"{suite_name}::{case_name} differs from its golden snapshot"
                )

            print(f"{golden.relative_to(root)}: ok")
