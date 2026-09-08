#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code, normalize_duckdb_progress
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_creates_ragnar_store_after_workspace_write_denial(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with McpClient(
            binary, SANDBOXED.serve(), environment, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"r": ["ragnar"]})
            assert last_tool_text(client) == "[prepared]"

            r = code(r"""
                ragnar::ragnar_store_create(
                  "knowledge.ragnar.duckdb",
                  embed = NULL
                )
                """)
            client.send(r=r)
            output = normalize_duckdb_progress(client)
            assert "knowledge.ragnar.duckdb" in output
            assert "Operation not permitted" in output
            for directory in (str(workspace.resolve()), str(workspace)):
                output = output.replace(directory, "<workspace>")
            client.transcript[-1]["result"]["content"][0]["text"] = output
            assert not (workspace / "knowledge.ragnar.duckdb").exists()

            # fmt: r
            r = code(r"""
                store_path <- file.path(tempdir(), "knowledge.ragnar.duckdb")
                store <- suppressMessages(ragnar::ragnar_store_create(
                  store_path,
                  embed = NULL
                ))
                stopifnot(DBI::dbIsValid(store@con), file.exists(store_path))
                writeLines("created store under the worker tempdir")
                """)
            client.send(r=r)
            assert (
                normalize_duckdb_progress(client)
                == "created store under the worker tempdir\n"
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
