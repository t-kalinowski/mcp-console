#!/usr/bin/env python3
"""Add shared navigation and API link targets to generated package pages."""

from pathlib import Path
import re

project = Path.cwd()
for page in project.rglob("*.qmd"):
    header, delimiter, body = page.read_text().partition("\n---\n")
    assert delimiter, f"Missing generated front matter: {page}"
    parent = "../" * len(page.relative_to(project).parts)
    navigation = (
        '<nav aria-label="Console documentation">'
        f'<a href="{parent}index.html">MCP Console</a> · '
        f'<a href="{parent}PYTHON.html">Python guide</a> · '
        f'<a href="{parent}r/index.html">R package</a></nav>\n\n'
    )
    # Great Docs 0.17 links to symbol fragments but omits their target IDs.
    if page.parent == project / "reference" and page.stem != "index":
        navigation += f'<span id="mcp_console.{page.stem}"></span>\n\n'

        # Keep Quarto's section IDs and add method anchors outside the headings
        # so the TOC does not copy them and create duplicate IDs.
        def member_anchor(match: re.Match[str]) -> str:
            name = match[2].replace("\\", "").removesuffix("()")
            anchors = f'<span id="mcp_console.{page.stem}.{name}"></span>\n'
            # Quarto drops leading underscores from these section IDs, while
            # Great Docs member summaries link to the original method name.
            if name.startswith("__"):
                anchors += f'<span id="{name}"></span>\n'
            return anchors + "\n" + match[0]

        body = re.sub(
            r"^(#+ )\[([^\]]+)\]\{([^}]*\bdoc-object-name\b[^}]*)\}",
            member_anchor,
            body,
            flags=re.MULTILINE,
        )
    page.write_text(header + delimiter + navigation + body)
