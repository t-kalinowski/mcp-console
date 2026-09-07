#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import tool_text
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite

PLATFORMS = {"linux"}


def test_evaluates_without_filesystem_isolation(binary: Path) -> Transcript:
    with McpClient(binary, ("serve", "--no-sandbox")) as client:
        client.initialize_and_list_tools()
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        assert (
            tool_text(client.send(r='writeLines("from R", "result.txt"); answer <- 42'))
            == "[done]"
        )
        assert (workspace / "result.txt").read_text() == "from R\n"
        assert tool_text(client.send(r="answer")) == "[1] 42\n"
        assert (
            tool_text(client.send(control="restart"))
            == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        assert tool_text(client.send(r='exists("answer")')) == "[1] FALSE\n"
        return client.finish()


def test_keeps_python_caches_private_between_direct_workers(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment = {**os.environ, "TMPDIR": directory}
        with (
            McpClient(binary, ("serve", "--no-sandbox"), environment) as first,
            McpClient(binary, ("serve", "--no-sandbox"), environment) as second,
        ):
            for client in (first, second):
                client.initialize_and_list_tools()
                result = client.send(
                    python=code("""
                    import os
                    from pathlib import Path

                    cache = Path(os.environ["MPLCONFIGDIR"])
                    cache.mkdir(parents=True, exist_ok=True)
                    marker = cache / "session-marker"
                    print(marker.exists())
                    """)
                )
                assert tool_text(result) == "False\n", result
                assert (
                    tool_text(
                        client.send(
                            python=code("""
                    marker.write_text("private")
                    print(marker.read_text())
                    """)
                        )
                    )
                    == "private\n"
                )
            first.send(control="restart")
            result = first.send(
                python=code("""
                import os
                from pathlib import Path

                print((Path(os.environ["MPLCONFIGDIR"]) / "session-marker").exists())
                """)
            )
            assert tool_text(result) == "False\n", result
            return first.finish() + second.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
