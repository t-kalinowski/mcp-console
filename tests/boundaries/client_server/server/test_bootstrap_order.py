"""Startup producer ordering through public MCP and target framing."""

import shlex
import sys
import time
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    last_result_text,
    wait_for_evaluation_output,
    wait_for_idle_output,
)
from support.allocations import AllocationProfile
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.requirements import WORKER, NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.resolvers import checkpoint_uv_environment, FIXTURES
from support.ssh import configure, peer_environment
from support.suites import run_this_suite


@requires(WORKER, NATIVE_FIXTURES)
def test_failed_replacement_preserves_startup_producer_order(binary: Path) -> list:
    for active in (False, True):
        for complete in (False, True):
            check_bootstrap_completion_requires_input_termination(
                binary, active=active, complete=complete
            )
    return check_deferred_startup_output(binary, exceed_limit=False)


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_rejects_excess_deferred_startup_output(binary: Path) -> list:
    return check_deferred_startup_output(binary, exceed_limit=True)


def check_deferred_startup_output(binary: Path, *, exceed_limit: bool) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        profile = resources.enter_context(closing(AllocationProfile(root)))
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        ready = resources.enter_context(closing(FifoCheckpoint.create(root / "ready")))
        events = resources.enter_context(
            closing(FifoCheckpoint.create(root / "events"))
        )
        published = resources.enter_context(
            closing(FifoCheckpoint.create(root / "published"))
        )
        received = resources.enter_context(
            closing(FifoCheckpoint.create(root / "received"))
        )
        receipt_release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "receipt-release"))
        )
        resuming = resources.enter_context(
            closing(FifoCheckpoint.create(root / "resuming"))
        )
        replacement = "mcp-console-ordering-fixture"
        environment, resolving, resolved = checkpoint_uv_environment(root, replacement)
        resources.callback(resolving.close)
        resources.callback(resolved.close)
        wrapper = root / "uv"
        wrapper.write_text(
            f"#!{sys.executable}\n"
            +
            # fmt: python
            code(f"""
                import os
                import sys
                from pathlib import Path

                if {replacement!r} in sys.argv:
                    with Path({str(resolving.path)!r}).open("wb", buffering=0) as gate:
                        assert gate.write(b"1") == 1
                    with Path({str(resolved.path)!r}).open("rb", buffering=0) as gate:
                        assert gate.read(1) == b"1"
                    print("intentional candidate failure", file=sys.stderr)
                    raise SystemExit(1)
                os.execv({str(FIXTURES / "checkpoint_uv")!r}, [{str(FIXTURES / "checkpoint_uv")!r}, *sys.argv[1:]])
                """),
        )
        wrapper.chmod(0o755)
        environment.update(peer_environment(root, "bootstrap-order"))
        environment.update(
            RETICULATE_UV=str(wrapper),
            RETICULATE_PYTHON="managed",
            CONSOLE_BOOTSTRAP_ORDER_BINARY=str(binary),
            CONSOLE_BOOTSTRAP_ORDER_ROOT=str(root),
            CONSOLE_BOOTSTRAP_ORDER_NOISE="32768" if exceed_limit else "8192",
        )
        environment.update(profile.environment)
        environment.update(
            MCP_CONSOLE_TEST_RELAY_READ_MATCH="ordered receipt",
            MCP_CONSOLE_TEST_RELAY_READ_BLOCKED=str(received.path),
            MCP_CONSOLE_TEST_RELAY_READ_RELEASE=str(receipt_release.path),
        )
        environment["MCP_CONSOLE_TEST_CONTROLLER_LIBRARIES"] = (
            environment.pop(LOADER_VARIABLE)
            + ":"
            + str(build_interposer(root, "relay_stdout_read_interposer"))
        )
        launcher = code("""
            import os
            import sys

            os.environ["MCP_CONSOLE_TEST_RELAY_READ_PID"] = str(os.getpid())
            loader = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"
            os.environ[loader] = os.environ.pop("MCP_CONSOLE_TEST_CONTROLLER_LIBRARIES")
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        peer = FIXTURES / "bootstrap_order_peer.py"
        (root / "ssh").write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, str(peer)]) + ' "$@"\n'
        )
        configure(local, remote, [str(binary)])
        with McpClient(
            Path(sys.executable),
            ("-c", launcher, str(binary), "serve", "--no-sandbox"),
            environment,
            local,
        ) as client:
            try:
                client.initialize_and_list_tools()
                ready.wait("built-in target transport ready", timeout=120)
                preparation = client.start_send(
                    requirements={"action": "set", "python": [replacement]}
                )
                resolving.wait(
                    "replacement has reserved bootstrap and environment", timeout=120
                )
                profile.start()
                events.release()
                published.wait(
                    "startup semantic frames published while replacement is held"
                )
                # The controller reader has enqueued the preceding output
                # before replacement can enqueue ResumeBootstrap. A producer
                # write alone leaves that ordering dependent on transport speed.
                received.wait("controller received the startup output")
                receipt_release.release()
                client.send(timeout_ms=0)
                assert last_result_text(client) == "[session is preparing requirements]"
                if exceed_limit:
                    sessions = local / ".agents/console/sessions"
                    with Events() as changes:

                        def dispatched_receipt() -> Path | None:
                            logs = list(sessions.glob("*/outputs/session.log"))
                            if not logs:
                                return None
                            (log,) = logs
                            changes.watch_file(log)
                            return (
                                log
                                if b"ordered receipt\n" in log.read_bytes()
                                else None
                            )

                        # The public raw log is written by the dispatcher after
                        # all preceding frames reach the deferred spool. A pipe
                        # read receipt alone leaves that backlog unprocessed.
                        wait_for_checkpoint(
                            dispatched_receipt,
                            "dispatcher recorded output after the deferred backlog",
                            root=sessions,
                            recursive=True,
                            client=client,
                            events=changes,
                        )
                resolved.release()
                # A fresh producer event can reach the dispatcher before the
                # reservation owner's ResumeBootstrap marker.
                resuming.release()
                client.receive(preparation)
                assert preparation["result"]["isError"], preparation
                assert (
                    "intentional candidate failure"
                    in preparation["result"]["content"][0]["text"]
                ), preparation
                if exceed_limit:
                    result = client.send(timeout_ms=0)
                    output = last_result_text(client)
                    assert result["isError"], result
                    assert "deferred bootstrap retention exceeds 16 MiB" in output
                    assert all(item["type"] == "text" for item in result["content"])
                    assert "after activation" not in output, output
                    client.finish()
                    return [{"excess_deferred_startup_output_fails_worker": True}]
                notice = "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']"
                content = []
                deadline = time.monotonic() + client.response_timeout
                while True:
                    result = client.send(timeout_ms=0)
                    assert not result["isError"], result
                    content.extend(result["content"])
                    output = "".join(
                        item["text"] for item in content if item["type"] == "text"
                    )
                    if (
                        notice in output
                        and "after resumption\n" in output
                        and output.endswith("[waiting for stdin]")
                    ):
                        break
                    assert time.monotonic() < deadline, "startup events did not resume"
                assert output.index(notice) < output.index("after activation\n"), repr(
                    output
                )
                prompt = '[input requested: "startup> "]'
                assert output.index("after activation\n") < output.index(prompt), repr(
                    output
                )
                image_index = next(
                    index
                    for index, item in enumerate(content)
                    if item["type"] == "image"
                )
                before_image = "".join(
                    item["text"]
                    for item in content[:image_index]
                    if item["type"] == "text"
                )
                after_image = "".join(
                    item["text"]
                    for item in content[image_index + 1 :]
                    if item["type"] == "text"
                )
                assert (
                    notice in before_image and "after activation\n" in before_image
                ), content
                assert prompt in after_image, content
                assert output.index(prompt) < output.index("after resumption\n"), repr(
                    output
                )
                images = [item for item in content if item["type"] == "image"]
                assert len(images) == 1 and images[0]["mimeType"] == "image/png", images
                _, largest = profile.stop()
                assert largest < 256 * 1024, (
                    f"deferred startup allocated a growing queue: {largest}"
                )
                client.send(stdin="continue\n", python="42")
                assert last_result_text(client) == "42\n", client.transcript[-1]
                client.finish()
                return [
                    {
                        "startup_output_image_and_input_wait_for_deferred_activation": True,
                        "failed_replacement_resumes_original_worker": True,
                    }
                ]
            finally:
                events.release()
                receipt_release.release()
                resolved.release()
                resuming.release()


def check_bootstrap_completion_requires_input_termination(
    binary: Path, *, active: bool, complete: bool
) -> None:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        local, remote = root / "controller", root / "remote"
        local.mkdir()
        remote.mkdir()
        finish = resources.enter_context(
            closing(FifoCheckpoint.create(root / "finish-bootstrap"))
        )
        environment = peer_environment(root, "bootstrap-input-completion")
        environment["CONSOLE_BOOTSTRAP_COMPLETE"] = str(int(complete))
        configure(local, remote, [str(binary)])
        with McpClient(binary, ("serve", "--no-sandbox"), environment, local) as client:
            try:
                client.initialize_and_list_tools()
                if active:
                    client.send(python="never_run = True")
                    assert (
                        last_result_text(client)
                        == '[input requested: "startup> "]\n[waiting for stdin]'
                    )
                else:
                    wait_for_idle_output(
                        client,
                        '[input requested: "startup> "]\n[waiting for stdin]',
                        "startup input before first cell",
                        completion_timeout_seconds=client.response_timeout,
                    )
                finish.release()
                wait_for_evaluation_output(
                    client,
                    lambda output: (
                        "worker completed with an outstanding input request" in output
                    ),
                    "bootstrap completion fails the managed-input boundary",
                    expected_error=True,
                )
                assert not (root / "cell-ran").exists(), (
                    "cell ran with startup input still outstanding"
                )
                client.finish()
            finally:
                finish.release()


if __name__ == "__main__":
    run_this_suite(__file__)
