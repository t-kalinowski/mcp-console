#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, UNPRIVILEGED, requires


@requires(POSIX, UNPRIVILEGED)
def test_failed_storage_retirement_blocks_replacement(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        owned = root / "owned"
        owned.mkdir()
        environment = dict(
            os.environ,
            PATH=str(root),
            RETICULATE_PYTHON=sys.executable,
            TMPDIR=str(owned),
        )
        environment.pop("R_HOME", None)
        temporary = None
        try:
            with McpClient(
                binary, ("serve", "--no-sandbox"), environment, root
            ) as client:
                client.initialize_and_list_tools()
                # fmt: python
                python = code("""
                    import os
                    from pathlib import Path

                    temporary = Path(os.environ["TMPDIR"])
                    Path("worker-temporary").write_text(str(temporary))
                    restricted = temporary / "restricted"
                    restricted.mkdir()
                    (restricted / "retained.txt").write_text("private contents")
                    restricted.chmod(0)
                    """)
                client.send(python=python)
                temporary = Path((root / "worker-temporary").read_text())
                assert temporary.is_relative_to(owned), temporary
                failure = client.send(python="os._exit(47)")
                assert failure["isError"], failure
                assert "cannot remove worker temporary directory" in last_result_text(
                    client
                )
                replacement = client.send(python="print('replacement ran')")
                assert replacement["isError"], replacement
                assert "worker is shutting down" in last_result_text(client)
                assert "replacement ran\n" not in last_result_text(client)
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert "cannot remove worker temporary directory" in stderr, stderr
                for record in transcript:
                    for content in record.get("result", {}).get("content", []):
                        if content["type"] == "text":
                            content["text"] = content["text"].replace(
                                str(temporary), "<worker temporary>"
                            )
                return transcript + [
                    {
                        "exit_status": 1,
                        "stderr": stderr.replace(str(temporary), "<worker temporary>"),
                    }
                ]
        finally:
            if temporary is not None:
                (temporary / "restricted").chmod(0o700)
