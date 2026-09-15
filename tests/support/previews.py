"""Assertions on public previews and their separately retained raw files."""

import re
from pathlib import Path

from support.client import McpClient
from support.evidence import compact_text

TEXT_BUDGET = 8 * 1024
OMISSION = re.compile(
    r"\n\[output preview: omitted (\d+) rendered UTF-8 bytes; [^\n]*\]\n"
)

CONTROL_OMISSION = re.compile(
    r"\[… omitted (\d+) rendered UTF-8 bytes; not retained …\]"
)


def assert_preview(
    text: str, emitted: str, *, pattern: re.Pattern[str] = OMISSION
) -> int:
    """Check the exact emitted prefix/suffix and accounting around one omission."""
    assert len(text.encode()) <= TEXT_BUDGET, len(text.encode())
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, text
    marker = matches[0]
    head, tail = text[: marker.start()], text[marker.end() :]
    assert head and emitted.startswith(head), (head[:100], emitted[:100])
    assert tail and emitted.endswith(tail), (tail[-100:], emitted[-100:])
    omitted = int(marker[1])
    assert len(head.encode()) + omitted + len(tail.encode()) == len(emitted.encode()), (
        len(head.encode()),
        omitted,
        len(tail.encode()),
        len(emitted.encode()),
    )
    return omitted


def session_directory(client: McpClient) -> Path:
    assert client.temporary_directory is not None
    return next(
        (Path(client.temporary_directory.name) / ".agents/console/sessions").iterdir()
    )


def cell_text(client: McpClient, call_id: int) -> str:
    return (
        (session_directory(client) / f"outputs/call-{call_id:06}.log")
        .read_bytes()
        .decode("utf-8", errors="replace")
    )


def normalize_preview_paths(client: McpClient) -> None:
    run_id = session_directory(client).name
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                block["text"] = block["text"].replace(run_id, "<run ID>")


def normalize_pipe_counts(client: McpClient) -> None:
    """Normalize variable pipe-fill counts after exact raw-file assertions."""
    normalize_preview_paths(client)
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text" and "zod expected " in block["text"]:
                text = block["text"]
                text = re.sub(
                    r"(omitted )\d+( rendered UTF-8 bytes)",
                    r"\1<omitted byte count>\2",
                    text,
                )
                text = re.sub(r"\d+( raw bytes retained)", r"<raw byte count>\1", text)
                text = re.sub(
                    r"(zod expected [^\n]* tail: )\d+", r"\1<pipe tail bytes>", text
                )
                block["text"] = text


def compact_previews(client: McpClient, *units: str) -> None:
    """Represent selected repetitions only after public assertions and path normalization."""
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text" and isinstance(block["text"], str):
                block["text"] = compact_text(block["text"], *units)
