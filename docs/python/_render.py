#!/usr/bin/env python3
"""Add shared navigation and API link targets to generated package pages."""

from pathlib import Path

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
    page.write_text(header + delimiter + navigation + body)
