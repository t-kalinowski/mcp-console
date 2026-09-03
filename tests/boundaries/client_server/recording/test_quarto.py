#!/usr/bin/env -S uv run --script

import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _support import McpClient, Transcript, r_test_environment, run_this_suite


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir", "quarto"}


class RenderedText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def test_renders_generated_document(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            ("serve",),
            environment,
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        (workspace / "render-value.txt").write_text("40\n", encoding="utf-8")
        r_source = (
            "  #| eval: false\n"
            'echo <- 0L\nrender_value <- as.integer(readLines("render-value.txt"))\n'
            'cat("executed-r=40\\n")'
        )
        client.send(r=r_source)
        assert client.transcript[-1]["result"]["content"] == [
            {"type": "text", "text": "executed-r=40\n"}
        ]
        source = (
            "  #| eval: false\n"
            'echo = """before\n````\n<div>not markdown</div>\nafter"""\n'
            'print(f"executed-python={int(r.render_value) + 2}")\n'
            "print(echo)"
        )
        client.send(python=source)
        python_result = client.transcript[-1]["result"]["content"][0]["text"]
        assert "executed-python=42" in python_result, python_result
        assert "<div>not markdown</div>" in python_result, python_result
        transcript = client._finish()

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        document = session / "transcript.qmd"
        document_text = document.read_text(encoding="utf-8")
        assert f"```{{r}}\n\n{r_source}\n```" in document_text
        assert f"`````{{python}}\n\n{source}\n`````" in document_text
        assert "zod:" not in document_text
        assert "zod python:" not in document_text
        assert "execute:" not in document_text
        assert "python-version:" not in document_text
        assert "  python-packages:" in document_text
        assert f"    root.dir: {workspace.resolve()}" in document_text
        rendering = subprocess.run(
            [
                "ir",
                "render",
                document.name,
                "--to",
                "html",
                "--output",
                "transcript.html",
            ],
            cwd=session,
            check=False,
            capture_output=True,
            text=True,
        )
        assert rendering.returncode == 0, {
            "stdout": rendering.stdout,
            "stderr": rendering.stderr,
            "document": document.read_text(encoding="utf-8"),
        }
        rendered = (session / "transcript.html").read_text(encoding="utf-8")
        rendered_text = RenderedText()
        rendered_text.feed(rendered)
        visible_text = "".join(rendered_text.parts)
        assert "MCP Console code cells" in rendered
        assert "executed-r=40" in visible_text
        assert "executed-python=42" in visible_text
        assert "not markdown" in visible_text
        assert "<div>not markdown</div>" not in rendered
        assert "zod:" not in rendered
        assert "zod python:" not in rendered
        transcript.append(
            {
                "quarto document": {
                    "executed client-authored R and Python cells through `ir`": True,
                    "selected Python through reticulate defaults": True,
                    "omitted recorded runtime results": True,
                    "kept Markdown-looking source inside a code block": True,
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
