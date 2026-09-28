"""One Python cell contract with optional R, followed by bridge-only checks."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import R, requires


def exercise_python(client: McpClient) -> tuple[str, ...]:
    # This exact sequence is used by every configuration. It deliberately uses
    # only the standard library and does not refer to the R/Python bridge.
    # fmt: python
    first = code("""
        import os
        import sys
        assert os.path.samefile(sys.executable, os.environ["MCP_CONSOLE_TEST_PYTHON"])
        assert os.path.realpath(sys.prefix) == os.path.realpath(
            os.environ["MCP_CONSOLE_TEST_PYTHON_PREFIX"]
        )
        peer_value = 40
        print("shared stdout")
        sys.stderr.write("shared stderr\\n")
        peer_value + 1
        """)
    # fmt: python
    failure = code("""
        peer_value += 2
        raise ValueError("peer runtime sentinel")
        """)
    # fmt: python
    recovery = code("""
        (
            peer_value,
            "_mcp_console" not in globals(),
            "_mcp_console" in sys.modules,
        )
        """)
    # fmt: python
    shadow = code("""
        exec = None
        eval = None
        compile = None
        peer_value
        """)
    # fmt: python
    after_shadow = code("""
        peer_value += 1
        peer_value
        """)
    output = []
    for source in (first, failure, recovery, shadow, after_shadow):
        response = client.send(python=source)
        assert not response.get("isError"), response
        output.append(last_result_text(client))
    assert output[0] == "shared stdout\nshared stderr\n41\n", output[0]
    assert "<mcp-console:python:e2>" in output[1], output[1]
    assert output[1].endswith("ValueError: peer runtime sentinel\n"), output[1]
    assert output[2:] == ["(42, True, True)\n", "42\n", "43\n"], output[2:]
    return tuple(output)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_python_contract_with_and_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    # The host must have R so the same test can compare both configurations.
    # "python-first" describes cell order, not late R initialization: current
    # workers still initialize R eagerly whenever the selected session has R.
    reference = None
    records = None
    for mode in ("without-r", "r-first", "python-first"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            # Keep the exact same interpreter in all three configurations.
            # An explicit selection bypasses managed Python package resolution.
            config.write_text(json.dumps({"python": sys.executable}), encoding="utf-8")
            environment = dict(
                os.environ,
                MCP_CONSOLE_TEST_PYTHON=sys.executable,
                MCP_CONSOLE_TEST_PYTHON_PREFIX=sys.prefix,
            )
            environment.pop("RETICULATE_PYTHON", None)
            if mode == "without-r":
                path = root / "empty-path"
                path.mkdir()
                environment["PATH"] = str(path)
                for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_UV"):
                    environment.pop(name, None)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if mode == "r-first":
                    # R execution itself must not force CPython initialization.
                    client.send(
                        r="stopifnot(!reticulate::py_available(initialize = FALSE))"
                    )
                    assert last_result_text(client) == "[done]"
                actual = exercise_python(client)
                if reference is None:
                    reference = actual
                else:
                    # Preserve and compare complete error text, not summaries.
                    assert actual == reference, (mode, actual, reference)
                if mode != "without-r":
                    # Only the mixed configurations add cross-language checks.
                    # The Python state above must remain the same state seen by R.
                    # fmt: r
                    r = code("""
                        stopifnot(identical(reticulate::py_eval("peer_value"), 43L))
                        peer_from_r <- 44L
                        """)
                    client.send(r=r)
                    assert last_result_text(client) == "[done]"
                    client.send(python="(int(r.peer_from_r), peer_value)")
                    assert last_result_text(client) == "(44, 43)\n"
                transcript = client.finish()[3:]
                if mode == "without-r":
                    records = transcript
    # One representative transcript; all common Python outputs were asserted
    # byte-for-byte equal above. Bridge checks assert their distinct results.
    assert records is not None
    return records
