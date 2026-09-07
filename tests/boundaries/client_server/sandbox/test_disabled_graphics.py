#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import assert_result_content, last_tool_text
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import Transcript, TranscriptWithCompanions
from support.suites import run_this_suite

PLATFORMS = {"darwin", "linux"}


def test_direct_workers_keep_plot_files_separate(
    binary: Path,
) -> Transcript | TranscriptWithCompanions:
    environment, rscript = r_test_environment()
    with tempfile.TemporaryDirectory() as directory:
        environment["TMPDIR"] = directory
        expected = [
            reference_plots(
                rscript,
                environment,
                source,
                width=4,
                height=3,
                dpi=100,
                pages=1,
            )[0]
            for source in ("plot(1:3)", "plot(3:1)")
        ]
        with (
            McpClient(binary, ("serve", "--no-sandbox"), environment) as first,
            McpClient(binary, ("serve", "--no-sandbox"), environment) as second,
        ):
            directories = []
            for client, values in ((first, "1:3"), (second, "3:1")):
                client.initialize_and_list_tools()
                # Keep both devices open under the same inherited TMPDIR.
                # fmt: r
                r = code(r"""
                    options(
                      console.plot.width = 4,
                      console.plot.height = 3,
                      console.plot.dpi = 100
                    )
                    plot(VALUES)
                    path <- attr(.Devices[[dev.cur()]], "filepath")
                    cat(dirname(path), "\n", sep = "")
                    invisible(readline("finish plot> "))
                    """).replace("VALUES", values)
                client.send(r=r)
                path, prompt = last_tool_text(client).split("\n", 1)
                assert (
                    prompt == '[input requested: "finish plot> "]\n[waiting for stdin]'
                )
                directories.append(Path(path))
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<managed plot directory>\n" + prompt
                )
            assert directories[0] != directories[1], directories

            for client, image in ((second, expected[1]), (first, expected[0])):
                client.send(stdin="\n")
                assert_result_content(client, [image])
            transcript = first.finish() + second.finish()
            if sys.platform == "linux":
                return TranscriptWithCompanions(transcript, {}, platform="linux")
            return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
