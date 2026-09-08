#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import r_test_environment
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_probes_ambient_reticulate_before_first_use_bootstrap(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).resolve().parents[3] / "fixtures" / "ambient_reticulate"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        library = temporary / "library"
        library.mkdir()
        record = temporary / "reticulate-calls"
        environment["MCP_CONSOLE_TEST_RETICULATE_RECORD"] = str(record)
        subprocess.run(
            [rscript.with_name("R"), "CMD", "INSTALL", f"--library={library}", fixture],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        record.write_text("", encoding="utf-8")
        for name in ("R_LIBS", "R_LIBS_SITE", "R_LIBS_USER"):
            environment[name] = str(library)
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in path.split(os.pathsep)
            if not any((Path(entry) / name).exists() for name in ("ir", "uv", "uvx"))
        )
        environment.pop("RETICULATE_UV", None)
        environment.pop("RETICULATE_PYTHON", None)

        with McpClient(
            binary, execution.serve(), environment, current_directory=temporary
        ) as client:
            client.initialize_and_list_tools()
            tools = client.transcript[-1]["result"]["tools"]
            assert "requirements" in tools[0]["inputSchema"]["properties"], tools
            assert record.read_text(encoding="utf-8").splitlines() == [
                "namespace:--probe"
            ], "initialization invoked reticulate bootstrap"

            result = client.send(r='stop("cell must not run")')
            assert result.get("isError") is True, result
            content = result["content"]
            assert len(content) == 1 and content[0]["type"] == "text", content
            output = content[0]["text"]
            assert "fixture ambient reticulate bootstrap failed" in output, output
            assert "cell must not run" not in output, output
            assert "uv_binary" in record.read_text(encoding="utf-8").splitlines()

            listed_again = client.request("tools/list")
            assert listed_again["result"]["tools"] == tools, listed_again
            listed_again["result"]["tools"] = "<unchanged from initialization>"
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
