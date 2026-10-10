"""Installed-object help through ordinary Python cells."""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.previews import assert_preview, normalize_preview_paths
from support.records import Transcript
from support.suites import run_this_suite


@executions(RUNTIME)
def test_short_object_help_is_discoverable_and_noninteractive(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            "Help on method_descriptor:\n\n"
            "upper(self, /) unbound builtins.str method\n"
            "    Return a copy of the string converted to uppercase.\n\n",
            python="help(str.upper)",
        )
        python_guidance = client.transcript[2]["result"]["tools"][0]["inputSchema"][
            "properties"
        ]["python"]["description"]
        assert "`help(object)`" in python_guidance, python_guidance
        assert "bare `help()` prompts" in python_guidance, python_guidance
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_long_object_help_retains_readable_raw_log(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code(r"""
                def documented():
                    pass


                documented.__doc__ = "🙂" * 1500 + "\nOnly in the omitted middle.\n" + "🙂" * 1500
                """),
        )
        client.send(python="help(documented)")
        output = last_tool_text(client)
        notice = re.search(r"; raw log: ([^\n]+)\]\n", output)
        assert notice is not None, output
        assert client.temporary_directory is not None
        raw_path = Path(client.temporary_directory.name) / notice[1]
        raw = raw_path.read_bytes().decode("utf-8")
        assert raw == (
            "Help on function documented in module __main__:\n\n"
            "documented()\n    "
            + "🙂" * 1500
            + "\n    Only in the omitted middle.\n    "
            + "🙂" * 1500
            + "\n\n"
        ), raw
        assert_preview(output, raw)
        assert "Only in the omitted middle." not in output, output

        # Read the advertised file through the worker's ordinary execution path.
        client.expect(
            "Only in the omitted middle.\n",
            # fmt: python
            python=code(f"""
                with open({json.dumps(notice[1])}, encoding="utf-8") as log:
                    documentation = log.read()
                print(documentation.splitlines()[4].strip())
                """),
        )
        read_arguments = client.transcript[-1]["send"]
        read_arguments["python"] = read_arguments["python"].replace(
            raw_path.parent.parent.name, "<run ID>"
        )
        normalize_preview_paths(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
