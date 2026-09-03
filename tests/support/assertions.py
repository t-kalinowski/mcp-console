import base64
import time
from pathlib import Path
from typing import Any

from support.client import McpClient
from support.records import ToolResult, TranscriptEntry

LARGE_OUTPUT_SIZE = 2 * 1024 * 1024


def tool_text(result: ToolResult) -> str:
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def last_tool_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert isinstance(result, dict), result
    return tool_text(result)


def last_result_text(client: McpClient) -> str:
    return client.transcript[-1]["result"]["content"][0]["text"]


def entry_result_text(entry: TranscriptEntry) -> str:
    result = entry["result"]
    assert isinstance(result, dict), result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def assert_large_output(output: str, prefix: str) -> None:
    expected = prefix + ("x" * LARGE_OUTPUT_SIZE)
    assert output.startswith(expected), (
        f"captured {len(output)} bytes without the complete {len(expected)}-byte payload"
    )
    barrier = output.removeprefix(expected)
    assert barrier and not barrier.strip("y"), "unexpected text after captured payload"


def large_output(prefix: str) -> str:
    return prefix + ("x" * LARGE_OUTPUT_SIZE) + ("y" * LARGE_OUTPUT_SIZE)


def remove_length_marker(output: str, marker_prefix: str) -> tuple[str, int]:
    marker_start = output.find(marker_prefix)
    assert marker_start >= 0, (
        f"raw output lost length marker {marker_prefix!r}: {output[-500:]!r}"
    )
    marker_end = output.find("\n", marker_start)
    if marker_end < 0:
        marker_end = len(output)
        after_marker = marker_end
    else:
        after_marker = marker_end + 1
    length = int(output[marker_start + len(marker_prefix) : marker_end])
    return output[:marker_start] + output[after_marker:], length


def assert_result_content(
    client: McpClient,
    expected: list[str | bytes],
    *,
    image_reference: str = "live Rscript page {page}",
) -> None:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == len(expected), (
        f"expected {len(expected)} content blocks, got "
        f"{[item.get('type') for item in content]}"
    )
    page = 0
    for item, expected_item in zip(content, expected):
        if isinstance(expected_item, str):
            assert item == {"type": "text", "text": expected_item}, item
            continue

        image = item
        assert image.keys() == {"type", "data", "mimeType"}, image
        assert image["type"] == "image", image
        assert image["mimeType"] == "image/png", image
        data = base64.b64decode(image["data"], validate=True)
        reference = image_reference.format(page=page + 1)
        assert data == expected_item, (
            f"plot bytes differ: worker returned {len(data)} bytes, "
            f"{reference} returned {len(expected_item)} bytes"
        )
        page += 1
        image["data"] = f"<PNG byte-identical to {reference}>"


def release_worker_callback_gate(
    client: McpClient,
    description: str,
    extra_path_labels: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    paths = content[0]["text"].splitlines()
    assert len(paths) == 2 + len(extra_path_labels), content
    content[0]["text"] = "\n".join(
        (
            "<worker callback gate>",
            "<worker callback checkpoint>",
            *(f"<worker callback {label}>" for label in extra_path_labels),
        )
    )

    gate, checkpoint, *extra_paths = map(Path, paths)
    gate.touch()
    deadline = time.monotonic() + 5
    while not checkpoint.exists():
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description} reached its checkpoint"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not reach its checkpoint"
        )
        time.sleep(0.01)
    return tuple(extra_paths)


def wait_for_idle_output(
    client: McpClient,
    expected: str,
    description: str,
    **send_arguments: Any,
) -> None:
    """Poll the public idle snapshot until a worker event reaches the server."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    while True:
        result = client.send(**send_arguments)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == "\n[idle]", output
        if time.monotonic() >= deadline:
            raise AssertionError(f"{description} did not reach the server")
        time.sleep(0.01)

    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]


def wait_for_evaluation_output(
    client: McpClient,
    expected: str,
    description: str,
    *,
    provisional: str = "\n[waiting for stdin]",
    **send_arguments: Any,
) -> None:
    """Poll past one exact provisional state and retain the submitted call."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    result = client.send(**send_arguments)
    while True:
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == provisional, repr(output)
        assert time.monotonic() < deadline, f"{description} did not complete"
        result = client.send(timeout_ms=3_000)

    calls = client.transcript[poll_start:]
    submitted = calls[0]
    submitted["result"] = calls[-1]["result"]
    client.transcript[poll_start:] = [submitted]


def collect_running_output(
    client: McpClient,
    description: str,
    *,
    timeouts_ms: tuple[int, ...],
    initial_cuts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Poll a running evaluation and retain its public output cuts."""
    assert timeouts_ms and all(timeout_ms > 0 for timeout_ms in timeouts_ms)
    running = "\n[running; poll with an empty send]"
    poll_start = len(client.transcript)
    cuts = [cut for cut in initial_cuts if cut]
    for attempt, timeout_ms in enumerate(timeouts_ms):
        result = client.send(timeout_ms=timeout_ms)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output.endswith(running):
            cut = output.removesuffix(running)
            if cut:
                cuts.append(cut)
            if attempt + 1 == len(timeouts_ms):
                raise AssertionError(
                    f"{description} remained running after {len(timeouts_ms)} polls: "
                    f"collected={''.join(cuts)!r}, last={output!r}"
                )
            continue

        if output != "[done]" or not cuts:
            cuts.append(output)
        break

    collected = "".join(cuts)
    content[0]["text"] = collected
    polls = client.transcript[poll_start:]
    submitted = polls[0]
    submitted["result"] = polls[-1]["result"]
    client.transcript[poll_start:] = [submitted]
    return tuple(cuts)


def assert_exact_interleaving(actual: str, first: str, second: str) -> None:
    assert len(actual) == len(first) + len(second), repr(actual)
    first_offsets = {0}
    for offset, character in enumerate(actual):
        next_offsets = set()
        for first_offset in first_offsets:
            second_offset = offset - first_offset
            if first_offset < len(first) and first[first_offset] == character:
                next_offsets.add(first_offset + 1)
            if second_offset < len(second) and second[second_offset] == character:
                next_offsets.add(first_offset)
        first_offsets = next_offsets
    assert len(first) in first_offsets, repr(actual)
