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
        client.send(r='cat("Installed R:", R.version.string, "\\n")')
        normalize_paths(client, root)
        client.send(r='RShowDoc("R-exts")')
        output = normalize_paths(client, root)
        assert "6 The R API: entry points for C code" in output, output

        client.send(r='RShowDoc("R-exts#The-R-API")')
        output = normalize_paths(client, root)
        assert "Do not\n  use these in distributed code" in output, output
        assert "#include <R.h>" in output, output
        assert "#API-index)" in output, output

        client.send(r='RShowDoc("R-exts#API-index")')
        output = normalize_paths(client, root)
        assert "intended to be used in distributed packages" in output, output
        assert "#API-index_ap_letter-R)" in output, output

        client.send(
            # fmt: r
            r=code(r"""
                # Inspect the same cached index with ordinary R file tools.
                index <- capture.output(path <- .console$manual("R-exts#API-index_ap_letter-R"))
                writeLines(grep(
                  "`Rf_allocVector`",
                  readLines(path, encoding = "UTF-8"),
                  value = TRUE,
                  fixed = TRUE
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
        command = re.search(r"Next file: (.+)", output)[1]
        client.send(r=command)
        output = normalize_paths(client, root)
        assert "R_alloc" in output, output
        return [scenario, *client.finish()]


def manual_workflow_csv_import(binary: Path, execution: Execution) -> Transcript:
    scenario = {
        "scenario": (
            "An analyst needs to import a semicolon-separated CSV with decimal "
            "commas and export it in the same format. Search and read the manual, "
            "then verify the documented functions on a small example."
        ),
        "documentation": (
            "HTML manuals from the running R installation. This client uses "
            "Console's R interface only, without the R client package or "
            "external filesystem tools."
        ),
    }
    with manual_client(binary, execution, installed=True) as (
        client,
        root,
    ):
        client.send(r='cat("Installed R:", R.version.string, "\\n")')
        normalize_paths(client, root)
        client.send(
            r='.console$manual_search("read.csv2", manual = "R-data", limit = 3L)'
        )
        output = normalize_paths(client, root)
        assert "2.1 Variations on read.table" in output, output
        read = re.search(r"  Read: (.+)", output)[1]
        location, arguments = (
            read.removeprefix(".console$manual(").removesuffix(")").split(", ", 1)
        )
        client.send(
            # fmt: r
            r=code(f"""
                .console$manual(
                  {
                    location
                  },
                  {
                    arguments
                  },
                  lines = 5L
                )
                """),
        )
        output = normalize_paths(client, root)
        assert "semicolons to separate fields" in " ".join(output.split()), output
        assert "read.csv2" in output, output

        client.send(
            r='.console$manual_search("Export to text files", manual = "R-data", limit = 1L)'
        )
        output = normalize_paths(client, root)
        assert "1.2 Export to text files" in output, output
        command = re.search(r"  Read: (.+)", output)[1]
        client.send(r=command)
        output = normalize_paths(client, root)
        if "write.csv2" not in output:
            command = re.search(r"(?:Continue|Next file): (.+)", output)[1]
            client.send(r=command)
            output = normalize_paths(client, root)
        assert "write.csv2" in output, output
        assert 'dec = ","' in output, output
        assert r'sep = "\t"' in client.transcript[-1]["result"]["content"][0]["text"]

        client.send(
            # fmt: r
            r=code(r"""
                csv <- "item;amount\ncoffee;1,25\ntea;2,50"
                prices <- read.csv2(text = csv)
                prices
                write.csv2(prices, row.names = FALSE)
                """),
        )
        output = normalize_paths(client, root)
        assert "coffee   1.25" in output, output
        assert '"coffee";1,25\n"tea";2,5' in output, output
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
    for workflow in (manual_workflow_c_api, manual_workflow_csv_import):
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
