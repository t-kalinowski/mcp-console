#!/usr/bin/env python3
"""Render the documentation website and check its public pages and links."""

from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent


class Page(HTMLParser):
    def __init__(self, path: Path) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.title = ""
        self.in_title = False
        self.feed(path.read_text())

    def handle_starttag(
        self, tag: str, attributes: list[tuple[str, str | None]]
    ) -> None:
        attrs = dict(attributes)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for name in ("href", "src"):
            if attrs.get(name):
                self.links.append(attrs[name])
        if tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data


class WebsiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = tempfile.TemporaryDirectory(prefix="console-website-")
        cls.addClassCleanup(cls.workspace.cleanup)
        docs = Path(cls.workspace.name) / "docs"
        shutil.copytree(
            ROOT / "docs", docs, ignore=shutil.ignore_patterns(".quarto", "_site")
        )
        result = subprocess.run(
            ["quarto", "render", str(docs), "--to", "html"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        cls.site = (docs / "_site").resolve()
        cls.pages = {path.resolve(): Page(path) for path in cls.site.rglob("*.html")}

    def test_guides_have_titles_navigation_and_search(self) -> None:
        search = json.loads((self.site / "search.json").read_text())
        indexed = {urlsplit(entry["href"]).path for entry in search}
        sources = [
            *(ROOT / "docs").glob("*.md"),
            *(ROOT / "docs/benchmarks").glob("*.md"),
        ]
        for source in sources:
            relative = source.relative_to(ROOT / "docs").with_suffix(".html")
            with self.subTest(page=str(relative)):
                page = self.pages[(self.site / relative).resolve()]
                heading = (
                    source.read_text()
                    .splitlines()[0]
                    .removeprefix("# ")
                    .replace("`", "")
                )
                self.assertIn(heading, page.title)
                self.assertIn("quarto-sidebar", page.ids)
                self.assertIn(relative.as_posix(), indexed)
        for name in ("index.html", "getting-started.html"):
            self.assertIn((self.site / name).resolve(), self.pages)
        self.assertFalse((self.site / "templates").exists())

    def test_local_links_and_anchors_resolve(self) -> None:
        self.assertTrue(self.pages, "the website has no rendered pages")
        for path, page in self.pages.items():
            for link in page.links:
                url = urlsplit(link)
                if url.scheme or url.netloc:
                    if url.netloc == "github.com" and url.path.startswith(
                        "/t-kalinowski/mcp-console/blob/main/"
                    ):
                        with self.subTest(
                            page=str(path.relative_to(self.site)), link=link
                        ):
                            self.assertNotIn("/../", url.path)
                            source = url.path.removeprefix(
                                "/t-kalinowski/mcp-console/blob/main/"
                            )
                            self.assertTrue(
                                (ROOT / unquote(source)).exists(),
                                "missing repository target",
                            )
                    continue
                target = unquote(url.path)
                if target.startswith("/"):
                    destination = self.site / target.removeprefix(
                        "/mcp-console/"
                    ).lstrip("/")
                else:
                    destination = path.parent / target if target else path
                if destination.is_dir():
                    destination /= "index.html"
                destination = destination.resolve()
                with self.subTest(page=str(path.relative_to(self.site)), link=link):
                    self.assertTrue(
                        destination.is_relative_to(self.site), "link escapes the site"
                    )
                    self.assertTrue(destination.is_file(), "missing link target")
                    if url.fragment and destination in self.pages:
                        self.assertIn(
                            unquote(url.fragment), self.pages[destination].ids
                        )


if __name__ == "__main__":
    unittest.main()
