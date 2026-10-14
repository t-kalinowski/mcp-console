#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12>=0.2.0", "joblib"]
# ///
"""Exercise installed R manuals and record complete YAML client workflows.

Unlike golden acceptance snapshots, these recordings retain the installed
manuals' version-dependent content. Both execution modes must agree on the
same host. Synthetic fixtures own deterministic regression coverage.
"""

import argparse
import re
from pathlib import Path

from boundaries.client_server.r.test_manuals import manual_client, normalize_paths
from support.execution import DIRECT, SANDBOXED, Execution
from support.normalization import code
from support.records import Transcript
from support.snapshots import (
    compact_initializations,
    format_transcript,
    initialization_reference,
    normalize_request_ids,
    root,
)


def manual_workflow_c_api(binary: Path, execution: Execution) -> Transcript:
    scenario = {
        "scenario": (
            "A package author asks whether an exported C symbol is necessarily "
            "supported, then checks Rf_allocVector in the API index and reads "
            "its allocation contract."
        ),
        "documentation": (
            "HTML manuals from the running R installation. The worker uses its "
            "ordinary converter and a private cache; external viewers raise errors."
        ),
    }
    with manual_client(binary, execution, installed=True) as (
        client,
        root,
    ):
        client.send(
            # fmt: r
            r=code(r"""
                cat("Installed R:", R.version.string, "\n")
                RShowDoc("R-exts")
                """),
        )
        output = normalize_paths(client, root)
        assert "6 The R API: entry points for C code" in output, output

        client.send(r='RShowDoc("R-exts#The-R-API")')
        output = normalize_paths(client, root)
        assert "Do not\n  use these in distributed code" in output, output
        assert "#include <R.h>" in output, output
        assert "#API-index)" in output, output
        continuation = re.search(r"Continue: (.+)", output)
        assert continuation, "the R API chapter must have a bounded continuation"
        first_page = output
        client.send(r=continuation[1])
        output = normalize_paths(client, root)
        assert "Location: Writing R Extensions > 6 The R API" in output, output
        assert len(output.encode("utf-8")) < 8192
        assert "Do not\n  use these in distributed code" not in output, output
        assert output != first_page

        client.send(
            # fmt: r
            r=code(r"""
                # Inspect the same cached index with ordinary R file tools.
                discard <- capture.output(path <- .console$manual("R-exts"))
                writeLines(grep(
                  "^\\|.*`Rf_allocVector`",
                  readLines(path, encoding = "UTF-8"),
                  value = TRUE
                ))
                """),
        )
        output = normalize_paths(client, root)
        assert "#index-Rf_005fallocVector)" in output, output
        assert "[Allocating storage]" in output, output

        client.send(r='utils::RShowDoc("R-exts#index-Rf_005fallocVector")')
        output = normalize_paths(client, root)
        assert "5.9.2 Allocating storage" in output, output
        assert "only does so for lists, expressions and character" in output, output
        if "R_alloc" not in output:
            command = re.search(r"Continue: (.+)", output)[1]
            client.send(r=command)
            output = normalize_paths(client, root)
        assert "R_alloc" in output, output
        return [scenario, *client.finish()]


def manual_workflow_argument_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    scenario = {
        "scenario": (
            "A package author wants to understand why a default argument sees a "
            "function's local binding, while an explicitly supplied argument "
            "sees the caller's binding. Start with the function reference, then "
            "consult the language manual for the evaluation rules and verify them."
        ),
        "documentation": (
            "Function help and HTML manuals from the running R installation. "
            "This client uses Console's R interface only, without the R client "
            "package or external filesystem tools."
        ),
    }
    with manual_client(binary, execution, installed=True) as (client, root):
        client.send(
            # fmt: r
            r=code(r"""
                cat("Installed R:", R.version.string, "\n")
                help("function", package = "base")
                """),
        )
        output = normalize_paths(client, root)
        assert "Function Definition" in output and "arglist" in output, output
        client.send(
            r='.console$manual_search("default arguments", manual = "R-lang", limit = 2L)'
        )
        output = normalize_paths(client, root)
        assert "4.3.3 Argument evaluation" in output, output
        command = re.search(r"  Read: (.+)", output)[1]
        client.send(r=command)
        output = normalize_paths(client, root)
        prose = " ".join(output.split())
        assert "evaluation frame of the calling function" in prose, output
        assert "evaluation frame of the function" in prose, output
        assert "lazy evaluation" in prose, output
        client.send(
            # fmt: r
            r=code(r"""
                y <- 1L
                f <- function(x = y) {
                  y <- 2L
                  x
                }
                c(default = f(), supplied = f(y))
                """),
        )
        output = normalize_paths(client, root)
        assert re.search(r"default\s+supplied\s+2\s+1", output), output
        return [scenario, *client.finish()]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", type=Path, default=root / "target/release/mcp-console"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--execution", choices=("direct", "sandbox", "both"), default="both"
    )
    options = parser.parse_args()
    binary = options.binary.resolve()
    assert binary.is_file(), binary
    options.output.mkdir(parents=True, exist_ok=True)
    modes = (
        (DIRECT, SANDBOXED)
        if options.execution == "both"
        else (DIRECT if options.execution == "direct" else SANDBOXED,)
    )
    reference = root / initialization_reference
    for workflow in (manual_workflow_c_api, manual_workflow_argument_evaluation):
        recordings = []
        for execution in modes:
            print(f"Running {workflow.__name__} [{execution.name}]", flush=True)
            transcript = normalize_request_ids(workflow(binary, execution))
            references = [
                path
                for path in (
                    reference,
                    *sorted(reference.parent.glob(f"{reference.stem}.*.yaml")),
                )
                if path.stem.endswith(".direct") == (execution == DIRECT)
            ]
            recordings.append(
                format_transcript(
                    compact_initializations(
                        transcript,
                        references,
                        execution=execution.name,
                        execution_specific=False,
                    )
                )
            )
        assert all(recording == recordings[0] for recording in recordings), (
            f"{workflow.__name__}: direct and sandbox output differ"
        )
        destination = options.output / f"{workflow.__name__}.yaml"
        destination.write_text(recordings[0], encoding="utf-8")
        print(f"Recorded {destination}", flush=True)
