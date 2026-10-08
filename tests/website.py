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
from xml.etree import ElementTree

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
        shutil.copytree(
            ROOT / "r",
            docs.parent / "r",
            ignore=shutil.ignore_patterns("docs", "*.Rcheck", "*.tar.gz"),
        )
        shutil.copytree(ROOT / "python", docs.parent / "python")
        for name in ("pyproject.toml", "great-docs.yml", "LICENSE"):
            shutil.copy2(ROOT / name, docs.parent / name)
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

    def test_r_package_subsite(self) -> None:
        home = (self.site / "r/index.html").resolve()
        reference = (self.site / "r/reference/console_tool.html").resolve()
        self.assertIn(home, self.pages, "pkgdown home page is missing")
        self.assertIn(reference, self.pages, "console_tool reference is missing")
        self.assertIn("mcp.console", self.pages[home].title)
        self.assertIn("console_tool", self.pages[reference].title)
        sitemap = ElementTree.parse(self.site / "r/sitemap.xml")
        urls = {
            element.text
            for element in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        }
        reference_url = (
            "https://t-kalinowski.github.io/mcp-console/r/reference/console_tool.html"
        )
        self.assertIn(reference_url, urls)
        search = json.loads((self.site / "r/search.json").read_text())
        self.assertIn(reference_url, {entry["path"] for entry in search})
        self.assertIn("arguments", self.pages[reference].ids)
        self.assertIn("../../index.html", self.pages[reference].links)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/r/R/console-tool.R",
            self.pages[reference].links,
        )
        for name in (
            "index.html",
            "README.html",
            "DEVELOPMENT.html",
            "benchmarks/transcript-concurrency.html",
        ):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(home, destinations)
                self.assertNotIn(
                    "https://github.com/t-kalinowski/mcp-console/blob/main/r/README.md",
                    self.pages[page].links,
                )

    def test_python_package_subsite(self) -> None:
        home = (self.site / "python/index.html").resolve()
        self.assertIn(home, self.pages, "Great Docs home page is missing")
        reference = (self.site / "python/reference/index.html").resolve()
        self.assertIn(reference, self.pages, "Python API index is missing")
        for name in (
            "MCPConsole",
            "AsyncMCPConsole",
            "Requirements",
            "chatlas.tool",
            "chatlas.register",
            "openai.responses_tool",
            "openai.agents_tool",
            "openai.agents_server",
            "anthropic.tool",
            "anthropic.tools",
            "codex.server",
        ):
            with self.subTest(symbol=name):
                page = (self.site / f"python/reference/{name}.html").resolve()
                self.assertIn(page, self.pages, "Python API page is missing")
                self.assertIn(name, self.pages[page].title)
        search = json.loads((self.site / "python/search.json").read_text())
        self.assertIn("reference/MCPConsole.html", {entry["href"] for entry in search})
        sitemap = ElementTree.parse(self.site / "python/sitemap.xml")
        urls = {
            element.text
            for element in sitemap.findall(
                ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
            )
        }
        self.assertIn(
            "https://t-kalinowski.github.io/mcp-console/python/reference/MCPConsole.html",
            urls,
        )
        self.assertTrue((self.site / "python/llms-full.txt").is_file())
        for name in ("index.html", "reference/MCPConsole.html"):
            with self.subTest(package_page=name):
                page = self.site / "python" / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                for target in ("index.html", "PYTHON.html", "r/index.html"):
                    self.assertIn(self.site / target, destinations)
        self.assertIn(
            "https://github.com/t-kalinowski/mcp-console/blob/main/python/mcp_console/_sync.py",
            {
                link.split("#")[0]
                for link in self.pages[
                    (self.site / "python/reference/MCPConsole.html").resolve()
                ].links
            },
        )
        for name in ("index.html", "README.html", "PYTHON.html"):
            with self.subTest(page=name):
                page = self.site / name
                destinations = {
                    (page.parent / unquote(urlsplit(link).path)).resolve()
                    for link in self.pages[page].links
                    if not urlsplit(link).scheme and not urlsplit(link).netloc
                }
                self.assertIn(home, destinations)

    def test_local_links_and_anchors_resolve(self) -> None:
        self.assertTrue(self.pages, "the website has no rendered pages")
        for path, page in self.pages.items():
            for link in page.links:
                url = urlsplit(link)
                same_site = (
                    url.netloc == "t-kalinowski.github.io"
                    and url.path.startswith("/mcp-console/")
                )
                if (url.scheme or url.netloc) and not same_site:
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
