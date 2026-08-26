#!/usr/bin/env -S uv run --script

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _support import McpClient, Transcript, run_this_suite


PLATFORMS = {"darwin"}
REQUIRED_COMMANDS = {"yamark"}


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
        client.send(sql="echo SELECT 42")
        client._request(
            "tools/call",
            name="send",
            arguments={
                "r": "echo rejected",
                "python": "echo rejected",
                "requirements": {"r": ["foo:", "foo#bar"]},
            },
        )
        transcript = client._finish()

        session = next((workspace / ".mcp-console" / "sessions").iterdir())
        quarto = (session / "transcript.qmd").read_text(encoding="utf-8")
        assert "```r\nemit image\n```" in quarto
        assert "```python\necho print('formatted')\n```" in quarto
        assert "```sql\necho SELECT 42\n```" in quarto
        assert '    - "foo:"' in quarto
        assert "    - foo#bar" in quarto
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
