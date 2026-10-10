"""Explicit trusted administration, never invoked by worker lookup or startup."""

import argparse
import hashlib
import io
import json
import re
import shutil
import stat
import uuid
import urllib.parse
import urllib.request
import zipfile
from html.parser import HTMLParser

# Deliberately bounded well above the current official ~4 MiB / ~14 MiB manual.
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_PAGE = 2 * 1024 * 1024
REQUIRED = (*MANUAL_INDEXES, "copyright.txt", "license.txt")


class DownloadPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.in_title = False
        self.row = None
        self.text_links = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag == "tr":
            self.row = {"text": "", "links": []}
        if tag == "a" and self.row is not None:
            self.row["links"].extend(value for key, value in attrs if key == "href")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
        if self.row is not None:
            self.row["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag == "tr" and self.row is not None:
            if "plain text" in self.row["text"].lower():
                self.text_links.extend(self.row["links"])
            self.row = None


def official_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "docs.python.org":
        raise ValueError(
            "documentation downloads must remain on https://docs.python.org"
        )
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_source(path: str | None, url: str, limit: int) -> tuple[bytes, str]:
    if path is not None:
        with open(path, "rb") as source:
            data = source.read(limit + 1)
        final_url = url
    else:
        opener = urllib.request.build_opener(OfficialRedirect())
        with opener.open(official_url(url), timeout=30) as source:
            final_url = official_url(source.geturl())
            data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("documentation input exceeds its byte limit")
    return data, final_url


def members(
    archive: zipfile.ZipFile, prefix: str
) -> list[tuple[zipfile.ZipInfo, Path]]:
    entries = archive.infolist()
    if (
        len(entries) > MAX_ENTRIES
        or sum(item.file_size for item in entries) > MAX_EXPANDED
    ):
        raise ValueError(
            "documentation archive exceeds its entry or expanded-byte limit"
        )
    seen = set()
    spellings = {}
    files = set()
    result = []
    for item in entries:
        name = item.orig_filename
        parts = name.rstrip("/").split("/")
        kind = stat.S_IFMT(item.external_attr >> 16)
        if (
            not name
            or "\\" in name
            or "\0" in name
            or parts[0] != prefix
            or name != "/".join(parts) + ("/" if item.is_dir() else "")
            or any(
                not part
                or part in {".", ".."}
                or ":" in part
                or part != part.rstrip(" .")
                or re.fullmatch(
                    r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part
                )
                for part in parts
            )
            or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
            or (kind == stat.S_IFDIR and not item.is_dir())
            or item.flag_bits & 1
            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
            or item.file_size > MAX_FILE
        ):
            raise ValueError(f"unsafe documentation archive member: {name!r}")
        for depth in range(1, len(parts) + 1):
            spelling = "/".join(parts[:depth])
            previous = spellings.setdefault(spelling.casefold(), spelling)
            if previous != spelling:
                raise ValueError(f"case-aliased documentation archive member: {name!r}")
        key = "/".join(parts).casefold()
        if key in seen:
            raise ValueError(f"duplicate documentation archive member: {name!r}")
        seen.add(key)
        if len(parts) == 1:
            if not item.is_dir():
                raise ValueError("documentation archive root must be a directory")
            continue
        relative = Path(*parts[1:])
        if not item.is_dir():
            files.add(relative.as_posix())
        result.append((item, relative))
    if not set(REQUIRED) <= files:
        raise ValueError(
            "documentation archive is missing its indexes, copyright or license"
        )
    return result


def prepare(archive_path: str | None, page_path: str | None) -> dict[str, object]:
    if os.environ.get("MCP_CONSOLE_SANDBOX"):
        raise ValueError(
            "prepare-python-docs is trusted host administration; run it outside evaluated cells"
        )
    minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    root = documentation_root()
    destination = root / minor
    if destination.exists():
        cached = python_docs()
        if cached is None:
            raise ValueError(
                "Python documentation cache is incomplete; remove it before preparation"
            )
        return {
            key: value
            for key, value in cached.items()
            if key not in {"directory", "python_version", "python_executable"}
        }
    page_url = f"https://docs.python.org/{minor}/download.html"
    page_bytes, page_final_url = read_source(page_path, page_url, MAX_PAGE)
    page = DownloadPage()
    page.feed(page_bytes.decode("utf-8"))
    release = re.search(
        r"Python (\d+\.\d+\.\d+) documentation", page.title, re.IGNORECASE
    )
    # Older documentation series include the patch in both ZIP and root names.
    # Use the selected page's link, retaining the official origin/minor boundary.
    links = [
        match
        for link in page.text_links
        if (
            match := re.fullmatch(
                rf"https://docs\.python\.org/{re.escape(minor)}/archives/"
                rf"(python-{re.escape(minor)}(?:\.\d+)?-docs-text)\.zip",
                urllib.parse.urljoin(page_final_url, link),
            )
        )
    ]
    if len(links) != 1 or release is None or not release[1].startswith(minor + "."):
        raise ValueError(
            "official download page does not identify this Python minor's plain-text ZIP and release"
        )
    source_url, archive_root = links[0].group(0, 1)
    data, final_url = read_source(archive_path, source_url, MAX_ARCHIVE)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        selected = members(archive, archive_root)
        receipt = {
            "python_minor": minor,
            "indexes": list(MANUAL_INDEXES),
            "download_page_release": release[1],
            "download_page_url": page_url,
            "download_page_final_url": page_final_url,
            "download_page_sha256": hashlib.sha256(page_bytes).hexdigest(),
            "source_url": source_url,
            "archive_final_url": final_url,
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "archive_bytes": len(data),
            "expanded_bytes": sum(i.file_size for i, _ in selected),
            "archive_entries": len(archive.infolist()),
            "input": "local archive and page"
            if archive_path is not None
            else "official download",
        }
        root.mkdir(parents=True, exist_ok=True)
        # No archive path reaches an existing destination. Publish only after
        # every member has been validated/decompressed and the receipt written.
        # Ordinary inherited directory permissions keep the worker account's
        # existing read access on Windows; native policy owns write denial.
        staging = root / (".prepare-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            manual = staging / "manual"
            manual.mkdir()
            for item, relative in selected:
                target = manual / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
            (staging / "manifest.json").write_text(
                json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
            )
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return receipt


parser = argparse.ArgumentParser()
parser.add_argument("--archive")
parser.add_argument("--download-page")
options = parser.parse_args()
if (options.archive is None) != (options.download_page is None):
    parser.error("--archive and --download-page must be supplied together")
try:
    print(json.dumps(prepare(options.archive, options.download_page)))
except (OSError, ValueError, zipfile.BadZipFile) as error:
    print(f"cannot prepare Python documentation: {error}", file=sys.stderr)
    sys.exit(1)
