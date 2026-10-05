#!/usr/bin/env -S uv run --script
"""MCP closure between completed discovery and the next preparation stage."""

import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite


@contextmanager
def closed_between_stages(
    binary: Path, *, lose_close: bool = False
) -> Iterator[tuple[McpClient, Path]]:
    with TemporaryDirectory(dir="/tmp") as directory, ExitStack() as resources:
        root = Path(directory)
        selected = root / "stage-python"
        selected.symlink_to(sys.executable)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        eof = resources.enter_context(closing(FifoCheckpoint.create(root / "eof")))
        environment = dict(os.environ, PATH=str(root), RETICULATE_PYTHON=selected.name)
        environment.pop("R_HOME", None)
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_stage_checkpoint")
                ),
                "MCP_CONSOLE_TEST_STARTUP_SELECTION": str(selected),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_REACHED": str(reached.path),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_RELEASE": str(release.path),
                "MCP_CONSOLE_TEST_STARTUP_EOF": str(eof.path),
            }
        )
        if lose_close:
            environment["MCP_CONSOLE_TEST_STARTUP_LOSE_CLOSE"] = "1"
            environment["MCP_CONSOLE_TEST_STARTUP_CLOSE_LOST"] = str(
                root / "close-lost"
            )
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                reached.wait("discovery completed; Python inspection not registered")
                client.stdin.close()
                eof.wait("MCP reader received EOF while inspection is withheld")
                release.release()
                yield client, root
            finally:
                release.release()


@requires(POSIX, NATIVE_FIXTURES)
def test_eof_between_discovery_and_python_inspection_exits_cleanly(
    binary: Path,
) -> list:
    with closed_between_stages(binary) as (client, _):
        _, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr
        return [
            {
                "closed_between": ["discovery", "Python inspection"],
                "exit_status": 0,
                "stderr": stderr,
            }
        ]


@requires(POSIX, NATIVE_FIXTURES)
def test_eof_between_stages_preserves_unconfirmed_preparation_close(
    binary: Path,
) -> list:
    with closed_between_stages(binary, lose_close=True) as (client, root):
        _, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert (root / "close-lost").exists(), stderr
        assert stderr == (
            "MCP connection closed during runtime preparation; resolver input closed\n"
        ), stderr
        return [{"close_receipt_lost": True, "exit_status": 1, "stderr": stderr}]


if __name__ == "__main__":
    run_this_suite(__file__)
