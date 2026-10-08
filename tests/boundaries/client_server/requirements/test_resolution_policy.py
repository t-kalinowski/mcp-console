"""Runtime callbacks cannot turn locked or explicit declarations into preparation."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_configuration import configure
from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import (
    ir_run_records,
    recording_uv_environment,
    uv_tool_run_requirements,
)
from support.snapshots import execution_snapshots
from support.suites import run_this_suite


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_r_policy_for_missing_and_explicit_packages(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for policy in ("automatic", "explicit", "startup_only", "disabled"):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            env, record = recording_fixture_r_environment(
                root, ("consolepolicyfixture",)
            )
            configure(
                root,
                {
                    "r": {"packages": [], "resolution": policy},
                    "python": sys.executable,
                    "languages": ["r"],
                },
            )
            with McpClient(
                binary, execution.serve("-c", "cache=host"), env, root
            ) as client:
                client.initialize_and_list_tools()
                client.send(requirements={"action": "get"})
                baseline = len(ir_run_records(record))
                client.expect("ready\n", r='retained <- 42; cat("ready\\n")')
                if policy == "automatic":
                    client.expect(
                        "TRUE\n",
                        r='cat(requireNamespace("consolepolicyfixture", quietly = TRUE), "\\n", sep = "")',
                    )
                    assert len(ir_run_records(record)) == baseline + 1
                else:
                    client.expect(
                        "FALSE\n",
                        r='cat(requireNamespace("consolepolicyfixture", quietly = TRUE), "\\n", sep = "")',
                    )
                    assert len(ir_run_records(record)) == baseline
                changed = client.send(
                    requirements={"r": ["consolepolicyfixture"]}, r='cat("prepared\\n")'
                )
                if policy in ("automatic", "explicit"):
                    assert not changed.get("isError"), changed
                    client.expect(
                        "TRUE\n",
                        r='cat(consolepolicyfixture::fixture(), "\\n", sep = "")',
                    )
                    assert len(ir_run_records(record)) == baseline + 1
                else:
                    assert (
                        changed.get("isError")
                        and "configuration" in changed["content"][0]["text"]
                    ), changed
                    assert len(ir_run_records(record)) == baseline
                client.expect("42\n", r='cat(retained, "\\n", sep = "")')
                records.extend(client.finish())
    return records


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_explicit_python_policy_rejects_reticulate_callbacks(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        env, record = recording_uv_environment(root)
        configure(
            root,
            {
                "r": {"resolution": "explicit", "packages": []},
                "python": {"managed": {"packages": ["six"], "resolution": "explicit"}},
                "languages": ["r", "python"],
            },
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "attached\n",
                r='stopifnot(identical(reticulate::py_eval("len([])"), 0L)); cat("attached\\n")',
            )
            initial = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert initial["python"] == ["six"], initial
            baseline = uv_tool_run_requirements(record)
            rejected = client.send(
                r='tryCatch(reticulate::py_require("console_missing_policy_fixture"), error = function(error) cat(conditionMessage(error), "\\n", sep = ""))'
            )
            assert rejected["content"][0]["text"].startswith(
                "runtime Python resolution is disabled by configuration for this operation (explicit);"
            ), rejected
            assert uv_tool_run_requirements(record) == baseline
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == initial
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
