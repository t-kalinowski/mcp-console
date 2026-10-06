"""Lazy R access to the running Python interpreter."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.suites import run_this_suite


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_direct_py_access_attaches_on_demand(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for getter in (
        "reticulate::py",
        "py",
        'get("py", envir = as.environment("package:reticulate"))',
    ):
        environment, _ = r_test_environment()
        with McpClient(
            binary,
            execution.serve("-c", f"python={json.dumps(sys.executable)}"),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.expect(
                    # fmt: python
                    python=code("""
                        bridge_value = "startup value"
                        bridge_object = object()
                        bridge_identity = id(bridge_object)
                        """),
                )
                # Conflict diagnostics can read active bindings. Keep the
                # first deliberate getter access in the following cell.
                client.expect(
                    # fmt: r
                    r=code("""
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        library(reticulate, warn.conflicts = FALSE)
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        """),
                )
                client.expect(
                    '[1] "startup value"\n',
                    # fmt: r
                    r=code("""
                        main <- GETTER
                        stopifnot(
                          identical(main$bridge_value, "startup value"),
                          identical(GETTER$bridge_value, "startup value"),
                          reticulate::py_available(initialize = FALSE)
                        )
                        bridge_from_r <- 42L
                        main$bridge_value
                        """).replace("GETTER", getter),
                )
                client.expect(
                    "bridge state retained\n",
                    # fmt: python
                    python=code("""
                        assert id(bridge_object) == bridge_identity
                        assert bridge_value == "startup value"
                        assert int(r.bridge_from_r) == 42
                        print("bridge state retained")
                        """),
                )
            records.extend(client.finish()[3:])
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
