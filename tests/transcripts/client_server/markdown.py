#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import (
    McpClient,
    Transcript,
    TranscriptWithCompanions,
    assert_result_content,
    code,
    r_test_environment,
    reference_plots,
    run_this_suite,
)


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"yamark"}


def test_records_real_mixed_language_session(
    binary: Path,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()

        # fmt: r
        r = code(r"""
            options(
              console.plot.width = 4,
              console.plot.height = 3,
              console.plot.dpi = 100
            )
            measurements <- data.frame(
              label = c("a", "b"),
              value = c(2L, 5L)
            )
            plot(measurements$value, type = "b")
            """)
        expected_plot = reference_plots(
            rscript,
            environment,
            r,
            width=4,
            height=3,
            dpi=100,
            pages=1,
        )
        client.send(r=r)
        assert_result_content(client, expected_plot)

        # fmt: python
        python = code("""
            int(r.measurements["value"].sum())
            """)
        client.send(python=python)
        assert client.transcript[-1]["result"]["content"] == [
            {"type": "text", "text": "7\n"}
        ]

        sql = code("""
            SELECT label, value * 10 AS scaled
            FROM measurements
            ORDER BY label
            """)
        client.send(sql=sql)
        sql_output = client.transcript[-1]["result"]["content"][0]["text"]
        assert '"a"' in sql_output and "20" in sql_output, sql_output
        assert '"b"' in sql_output and "50" in sql_output, sql_output
        transcript = client._finish()

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "internal" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifacts = [event for event in events if event["event"] == "artifact_created"]
        assert len(artifacts) == 1, artifacts
        artifact = artifacts[0]
        assert (session / artifact["path"]).read_bytes() == expected_plot[0]

        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "[Artifact 1 from call 1]" in markdown
        assert "![Artifact 1]" in markdown
        assert artifact["path"] in markdown
        assert f"```{{r}}\n{r}```" in quarto
        assert f"```{{python}}\n{python}```" in quarto
        assert f"```{{sql}}\n{sql}```" in quarto
        assert "Artifact 1" not in quarto
        assert "execute:" not in quarto
        assert markdown.endswith("\n")
        assert quarto.endswith("\n")

        session_event = events[0]
        markdown = markdown.replace(session_event["run_id"], "<run ID>")
        markdown = markdown.replace(
            session_event["working_directory"],
            "<workspace>",
        )
        quarto = quarto.replace(
            session_event["working_directory"],
            "<workspace>",
        )
        for event in events:
            markdown = markdown.replace(event["at"], "<UTC timestamp>")

        return TranscriptWithCompanions(
            transcript=transcript,
            companions={"md": markdown, "qmd": quarto},
        )


def test_emits_yamark_formatted_documents(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        client.send(r="emit image")
        client.send(python="echo print('formatted')")
        client._request(
            "tools/call",
            name="send",
            arguments={"sql": "--| eval: false\necho SELECT 42", "typo": True},
        )
        client._request(
            "tools/call",
            name="send",
            arguments={
                "r": "echo rejected",
                "python": "echo rejected",
                "requirements": {
                    "r": [
                        "foo:",
                        "foo#bar",
                        "line\u2028separator",
                        "paragraph\u2029separator",
                    ]
                },
            },
        )
        transcript = client._finish()

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "```{r}\nemit image\n```" in quarto
        assert "```{python}\necho print('formatted')\n```" in quarto
        assert "```{sql}\n\n--| eval: false\necho SELECT 42\n```" in quarto
        assert "execute:" not in quarto
        assert '    - "foo:"' in quarto
        assert "    - foo#bar" in quarto
        assert '    - "line\\Lseparator"' in quarto
        assert '    - "paragraph\\Pseparator"' in quarto
        formatting = subprocess.run(
            [
                "yamark",
                "format",
                "--diff",
                "--wrap",
                "sentence",
                "--skip-embedded-formatters",
                "transcript.md",
                "transcript.qmd",
            ],
            cwd=session,
            check=False,
            capture_output=True,
            text=True,
        )
        assert formatting.returncode == 0, {
            "stdout": formatting.stdout,
            "stderr": formatting.stderr,
            "transcript.md": (session / "transcript.md").read_text(encoding="utf-8"),
            "transcript.qmd": (session / "transcript.qmd").read_text(encoding="utf-8"),
        }
        transcript.append(
            {
                "generated documents": {
                    "transcript.md": "Yamark formatted",
                    "transcript.qmd": "Yamark formatted",
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
