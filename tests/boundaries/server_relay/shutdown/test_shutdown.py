#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server_relay._harness import (
    CAPTURE_NAME,
    EVALUATION_OUTPUT_READY_NAME,
    FifoCheckpoint,
    PENDING_TEXT_BUDGET,
    PNG_1X1,
    PRELUDE_PROCESSED_NAME,
    PRELUDE_RELEASE_NAME,
    Path,
    RETIREMENT_RELEASE_NAME,
    SHUTDOWN_RECEIVED_NAME,
    ServerRelayClient,
    Transcript,
    _fake_ir_environment,
    _normalize_shutdown_grace,
    _receive_checkpointed,
    _tool_text,
    os,
    run_this_suite,
    stop_client,
    sys,
    tempfile,
)


PLATFORMS = {"darwin"}


def test_gracefully_shuts_down(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "shutdown")
    assert _tool_text(client.send(control="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_shutdown()


def test_shutdown_precedes_blocked_resolver_cancellation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "blocked-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint(root / "resolver-started", create=True)
        resolver_release = root / "resolver-release"
        os.mkfifo(resolver_release)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release)

        client = ServerRelayClient(
            binary,
            "blocked_live_r_resolver_shutdown",
            environment,
        )
        client.start_worker()
        relay_root = client.relay_root()
        capture = (relay_root / CAPTURE_NAME).open(encoding="utf-8")
        shutdown_received = FifoCheckpoint(relay_root / SHUTDOWN_RECEIVED_NAME)
        retirement_release = FifoCheckpoint(relay_root / RETIREMENT_RELEASE_NAME)
        finished = False
        try:
            preparation = client.client._start_send(
                r="must not execute during shutdown",
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait()
            client.client.stdin.close()
            shutdown_received.wait()
            # Shutdown receipt precedes resolver cancellation. This response
            # proves the cancelled resolver callback has now returned.
            _receive_checkpointed(
                client.client,
                preparation,
                "the cancelled R preparation",
            )
            result = preparation["result"]
            assert result.get("isError") is True, result
            assert result["content"] == [
                {"type": "text", "text": "R package resolution cancelled"}
            ], result
            retirement_release.release()
            client.client._finish()
            finished = True
            transcript = client._read_open_capture(capture)
        finally:
            if not finished:
                stop_client(client.client)
            capture.close()
            shutdown_received.close()
            retirement_release.close()
            resolver_started.close()
            client._temporary.cleanup()

    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert server_commands == shutdown, server_commands
    return transcript


def test_cancelled_send_returns_owned_output_to_restart(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "cancelled_waiting_send")
    client.start_worker()
    relay_root = client.relay_root()
    prelude_release = FifoCheckpoint(relay_root / PRELUDE_RELEASE_NAME)
    prelude_processed = FifoCheckpoint(relay_root / PRELUDE_PROCESSED_NAME)
    output_ready = FifoCheckpoint(relay_root / EVALUATION_OUTPUT_READY_NAME)
    shutdown_received = FifoCheckpoint(relay_root / SHUTDOWN_RECEIVED_NAME)
    retirement_release = FifoCheckpoint(relay_root / RETIREMENT_RELEASE_NAME)
    finished = False
    retirement_released = False
    try:
        prelude_release.release()
        prelude_processed.wait()

        waiting = client.client._start_send(r="42", timeout_ms=30_000)
        output_ready.wait()
        restart = client.client._start_send(control="restart")
        shutdown_received.wait()
        client.client._notify(
            "notifications/cancelled",
            requestId=waiting["id"],
            reason="acceptance test cancelled the waiting send",
        )
        cancellation = client.client.transcript[-1]["input"]["params"]
        assert cancellation["requestId"] == waiting["id"], cancellation
        cancellation["requestId"] = "<request ID>"
        retirement_release.release()
        retirement_released = True
        client.client._receive(restart)

        assert "result" not in waiting, waiting
        result = restart["result"]
        assert result["isError"] is True, result
        assert [content["type"] for content in result["content"]] == [
            "text",
            "image",
            "text",
            "image",
            "text",
        ], result
        assert result["content"][0]["text"] == "idle before image\n", result
        assert result["content"][1] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result
        assert result["content"][2]["text"] == (
            "idle after image\n[output produced while idle]\ncell before image\n"
        ), result
        assert result["content"][3] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result

        cell_prefix = "cell before image\n"
        retained = "x" * (PENDING_TEXT_BUDGET - len(cell_prefix))
        omitted = len(cell_prefix) + 7
        truncation = (
            f"[output truncated: omitted {omitted} text bytes and "
            "0 encoded image bytes across 1 event]"
        )
        tail = result["content"][4]["text"]
        assert tail.startswith(retained + "\n" + truncation), (
            f"unexpected reclaimed tail: length={len(tail)}, tail={tail[-500:]!r}"
        )
        for notice in (
            truncation,
            "[stopped by session restart request before evaluation finished]",
            "[worker stopped: in-memory state lost]",
            "[active evaluation stopped by session restart request]",
            "[starting new worker]",
            "[idle]",
        ):
            assert tail.count(notice) == 1, (notice, tail[-1_000:])
        result["content"][4]["text"] = tail.replace(
            retained,
            f"<retained {len(retained)} text bytes>",
            1,
        )

        client.send()
        assert _tool_text(client.client.transcript[-1]["result"]) == "\n[idle]"
        transcript = client.client._finish()
        finished = True
        return transcript
    finally:
        if not retirement_released:
            retirement_release.release()
        if not finished:
            stop_client(client.client)
        prelude_release.close()
        prelude_processed.close()
        output_ready.close()
        shutdown_received.close()
        retirement_release.close()
        client._temporary.cleanup()


if __name__ == "__main__":
    run_this_suite(__file__)
