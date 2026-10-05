"""Portable relay lifecycle scenarios, observed through the public JSONL seam."""

LIFECYCLE_COMMANDS = {
    "invalid_sideband": {"kind": "evaluate", "language": "r", "source": "42"},
    "closed_stdin": {"kind": "stdin", "data": "hello"},
    "exit_tail": {"kind": "evaluate", "language": "r", "source": "42"},
    "exit_invalid": {"kind": "evaluate", "language": "r", "source": "42"},
}


def assert_failure_tail(
    events: list[dict], diagnostic: str, outcome: dict
) -> list[dict]:
    assert len(events) == 5, events
    fatal = events[2]
    assert fatal["kind"] == "fatal" and diagnostic in fatal["message"], events
    assert events == [
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        fatal,
        {"kind": "worker_sideband_closed"},
        outcome,
    ], events
    return events


def assert_exit_tail(events: list[dict], *, invalid: bool = False) -> dict:
    output = [
        {"kind": "console_output", "data": f"{index:04}"} for index in range(1024)
    ]
    assert events[:1024] == output, events
    if invalid:
        tail = assert_failure_tail(
            events[1024:],
            "worker sideband read failed: unknown variant `broken`",
            {"kind": "worker_exited", "code": 0},
        )
    else:
        tail = [
            {"kind": "image", "data": "eA==", "mime_type": "image/png"},
            {"kind": "completed"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ]
        assert events[1024:] == tail, events
    # Compact only after asserting every payload and the complete terminal tail.
    return {
        "output_count": len(output),
        "first": output[0],
        "last": output[-1],
        "tail": tail,
    }
