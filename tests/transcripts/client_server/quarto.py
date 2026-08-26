#!/usr/bin/env -S uv run --script

import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"ir", "quarto"}


class RenderedText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def test_renders_generated_document(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[2] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            ("serve", "--worker", str(zod)),
            current_directory=workspace,
        )
        client._initialize_and_list_tools()
        r_source = 'echo <- 0L\nrender_value <- 40L\ncat("executed-r=40\\n")'
        client.send(r=r_source)
        source = (
            'echo = """before\n````\n<div>not markdown</div>\nafter"""\n'
            'print(f"executed-python={int(r.render_value) + 2}")\n'
            "print(echo)"
        )
        client.send(python=source)
        transcript = client._finish()

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        document = session / "transcript.qmd"
        document_text = document.read_text(encoding="utf-8")
        assert f"```{{r}}\n{r_source}\n```" in document_text
        assert f"`````{{python}}\n{source}\n`````" in document_text
        assert "zod:" not in document_text
        assert "zod python:" not in document_text
        assert "execute:" not in document_text
        assert "python-version:" not in document_text
        assert "  python-packages:" in document_text
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
                    "executed client-authored R and Python cells through IR": True,
                    "selected Python through reticulate defaults": True,
                    "omitted recorded runtime results": True,
                    "kept Markdown-looking source inside a code block": True,
                }
            }
        )
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
