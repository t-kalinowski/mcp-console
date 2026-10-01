"""Startup producer ordering through public MCP and target framing."""

import shlex
import sys
import time
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.normalization import code
from support.requirements import WORKER, requires
from support.resolvers import checkpoint_uv_environment, FIXTURES
from support.ssh import configure, peer_environment
from support.suites import run_this_suite


@requires(WORKER)
def test_failed_replacement_preserves_startup_producer_order(binary: Path) -> list:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
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
        )
        peer = FIXTURES / "bootstrap_order_peer.py"
        (root / "ssh").write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, str(peer)]) + ' "$@"\n'
        )
        configure(local, remote, [str(binary)])
        with McpClient(binary, ("serve", "--no-sandbox"), environment, local) as client:
            try:
                client.initialize_and_list_tools()
                ready.wait("built-in target transport ready", timeout=120)
                preparation = client.start_send(
                    requirements={"action": "set", "python": [replacement]}
                )
                resolving.wait(
                    "replacement has reserved bootstrap and environment", timeout=120
                )
                events.release()
                published.wait(
                    "startup semantic frames published while replacement is held"
                )
                client.send(timeout_ms=0)
                assert last_result_text(client) == "[session is preparing requirements]"
                resolved.release()
                client.receive(preparation)
                assert preparation["result"]["isError"], preparation
                assert (
                    "intentional candidate failure"
                    in preparation["result"]["content"][0]["text"]
                ), preparation
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
                    if notice in output and output.endswith("[waiting for stdin]"):
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
                images = [item for item in content if item["type"] == "image"]
                assert len(images) == 1 and images[0]["mimeType"] == "image/png", images
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
                resolved.release()


if __name__ == "__main__":
    run_this_suite(__file__)
