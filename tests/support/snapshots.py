import difflib
import json
from collections.abc import Iterator
from pathlib import Path

from yaml12 import Yaml, format_yaml, parse_yaml, read_yaml

from support.records import Transcript, TranscriptWithCompanions, YamlStream


root = Path(__file__).resolve().parents[2]
snapshot_directory = root / "tests" / "snapshots"
initialization_suite = "client_server/server/test_tools"
initialization_case = "initializes_and_lists_tools"


def snapshot_path(suite_name: str, case_name: str) -> Path:
    return snapshot_directory / suite_name / f"{case_name}.yaml"


initialization_reference = (
    snapshot_path(initialization_suite, initialization_case)
    .relative_to(root)
    .as_posix()
)


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


def check_snapshot(
    snapshot: Path, actual: YamlStream, case: str, *, update: bool
) -> None:
    actual_text = format_transcript(actual)

    if update:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(actual_text, encoding="utf-8")
        print(f"updated {snapshot.relative_to(root)}", flush=True)
        return
    if not snapshot.exists():
        raise SystemExit(
            f"{snapshot.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = read_yaml(snapshot, multi=True)
    if not identical(actual, expected):
        expected_text = format_transcript(expected)
        difference = "".join(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                actual_text.splitlines(keepends=True),
                fromfile=str(snapshot.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{difference}{case} differs from its snapshot")


def check_text_snapshot(
    snapshot: Path, actual: str, case: str, *, update: bool
) -> None:
    if update:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(actual, encoding="utf-8")
        print(f"updated {snapshot.relative_to(root)}", flush=True)
        return
    if not snapshot.exists():
        raise SystemExit(
            f"{snapshot.relative_to(root)} is missing; run scripts/test --update {case}"
        )

    expected = snapshot.read_text(encoding="utf-8")
    if actual != expected:
        difference = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(snapshot.relative_to(root)),
                tofile="actual",
            )
        )
        raise SystemExit(f"{difference}{case} differs from its snapshot")


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
    recorded: Transcript | TranscriptWithCompanions,
    *,
    update: bool,
) -> set[Path]:
    snapshot = snapshot_path(suite_name, case_name)
    case = f"{suite_name}::{case_name}"
    if isinstance(recorded, TranscriptWithCompanions):
        actual = without_request_ids(recorded.transcript)
        companions = []
        for name, contents in recorded.companions.items():
            assert name and Path(name).name == name and not name.startswith("."), name
            assert name in {"md", "qmd"} or name.endswith(".yaml"), name
            companions.append((snapshot.with_suffix(f".{name}"), contents))
    else:
        actual = without_request_ids(recorded)
        companions = []
    if snapshot != root / initialization_reference:
        reference = without_request_ids(
            read_yaml(root / initialization_reference, multi=True)
        )
        assert reference, f"{initialization_reference} contains no documents"
        if identical(actual[: len(reference)], reference):
            actual = [
                Yaml(initialization_reference, tag="!same-as"),
                *actual[len(reference) :],
            ]
    check_snapshot(snapshot, actual, case, update=update)
    checked = {snapshot}
    for companion, contents in companions:
        if isinstance(contents, str):
            check_text_snapshot(companion, contents, case, update=update)
        else:
            assert companion.suffix == ".yaml", companion
            check_snapshot(companion, contents, case, update=update)
        checked.add(companion)
    return checked
