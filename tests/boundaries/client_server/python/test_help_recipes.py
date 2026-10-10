"""Installed-object help through ordinary Python cells."""

import hashlib
import json
import os
import shlex
import shutil
import stat
import struct
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.previews import assert_preview, normalize_preview_paths
from support.records import Transcript
from support.requirements import POSIX, Requirement, requires
from support.snapshots import execution_snapshots
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import environment


@executions(RUNTIME)
def test_short_object_help_is_discoverable_and_noninteractive(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            "Help on method_descriptor:\n\n"
            "upper(self, /) unbound builtins.str method\n"
            "    Return a copy of the string converted to uppercase.\n\n",
            python="help(str.upper)",
        )
        python_guidance = client.transcript[2]["result"]["tools"][0]["inputSchema"][
            "properties"
        ]["python"]["description"]
        assert "`help(object)`" in python_guidance, python_guidance
        assert "bare `help()` prompts" in python_guidance, python_guidance
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_long_object_help_retains_readable_raw_log(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code(r"""
                def documented():
                    pass


                documented.__doc__ = "🙂" * 1500 + "\nOnly in the omitted middle.\n" + "🙂" * 1500
                """),
        )
        client.send(python="help(documented)")
        output = last_tool_text(client)
        notice = re.search(r"; raw log: ([^\n]+)\]\n", output)
        assert notice is not None, output
        assert client.temporary_directory is not None
        raw_path = Path(client.temporary_directory.name) / notice[1]
        raw = raw_path.read_bytes().decode("utf-8")
        assert raw == (
            "Help on function documented in module __main__:\n\n"
            "documented()\n    "
            + "🙂" * 1500
            + "\n    Only in the omitted middle.\n    "
            + "🙂" * 1500
            + "\n\n"
        ), raw
        assert_preview(output, raw)
        assert "Only in the omitted middle." not in output, output

        # Read the advertised file through the worker's ordinary execution path.
        client.expect(
            "Only in the omitted middle.\n",
            # fmt: python
            python=code(f"""
                with open({json.dumps(notice[1])}, encoding="utf-8") as log:
                    documentation = log.read()
                print(documentation.splitlines()[4].strip())
                """),
        )
        read_arguments = client.transcript[-1]["send"]
        read_arguments["python"] = read_arguments["python"].replace(
            raw_path.parent.parent.name, "<run ID>"
        )
        normalize_preview_paths(client)
        return client.finish()


def manual_fixture(
    root: Path, extra: tuple[str, bytes] | None = None, *, minor: str | None = None
) -> tuple[Path, Path]:
    minor = minor or f"{sys.version_info.major}.{sys.version_info.minor}"
    prefix = f"python-{minor}-docs-text/"
    page = root / "download.html"
    page.write_text(
        f"<title>Download — Python {minor}.99 documentation</title>"
        f'<table><tr><td>Plain text</td><td><a href="archives/python-{minor}-docs-text.zip">Download</a></td></tr></table>',
        encoding="utf-8",
    )
    archive = root / "manual.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, text in {
            "contents.txt": "Python Documentation contents\n",
            "library/index.txt": "The Python standard library\n* json\n",
            "library/json.txt": "json — JSON encoder and decoder\njson.loads decodes a JSON document.\n",
            "tutorial/index.txt": "The Python Tutorial\n",
            "reference/index.txt": "The Python Language Reference\n",
            "copyright.txt": "Python Software Foundation copyright fixture\n",
            "license.txt": "Python Software Foundation license fixture\n",
        }.items():
            bundle.writestr(prefix + name, text)
        if extra is not None:
            bundle.writestr(*extra)
    return page, archive


def prepare_manual(
    binary: Path, root: Path, page: Path, archive: Path, *, python: str = sys.executable
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(binary),
            "prepare-python-docs",
            "--python",
            python,
            "--download-page",
            str(page),
            "--archive",
            str(archive),
        ],
        env={**os.environ, "MCP_CONSOLE_PYTHON_DOCS": str(root)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_manual_cache_is_worker_local_and_version_matched(
    binary: Path, execution: Execution
) -> Transcript:
    # Persistent host storage stays outside the native writable temp/workspace roots.
    with tempfile.TemporaryDirectory(
        prefix="console-manual-", dir=Path.home()
    ) as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        tools = root / "tools"
        tools.mkdir()
        cache = root / "manuals"
        env = {
            **environment(tools),
            "MCP_CONSOLE_PYTHON_DOCS": str(cache),
            "MCP_CONSOLE_HOME": str(root / "console"),
        }
        page, archive = manual_fixture(root)
        prepared = prepare_manual(binary, cache, page, archive)
        assert prepared.returncode == 0, prepared.stderr
        receipt = json.loads(prepared.stdout)
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert receipt["python_minor"] == minor, receipt
        assert receipt["download_page_release"] == minor + ".99", receipt
        assert receipt["indexes"] == [
            "contents.txt",
            "library/index.txt",
            "tutorial/index.txt",
            "reference/index.txt",
        ], receipt
        assert (
            receipt["archive_sha256"]
            == hashlib.sha256(archive.read_bytes()).hexdigest()
        ), receipt
        assert (
            receipt["source_url"]
            == f"https://docs.python.org/{minor}/archives/python-{minor}-docs-text.zip"
        ), receipt
        original = (cache / minor / "manifest.json").read_bytes()
        # Reuse must not read the now missing download inputs or fetch the network.
        page.unlink()
        archive.unlink()
        reused = prepare_manual(binary, cache, page, archive)
        assert reused.returncode == 0, reused.stderr
        assert json.loads(reused.stdout) == receipt
        assert (cache / minor / "manifest.json").read_bytes() == original
        with McpClient(
            binary,
            execution.serve("-c", "python=" + json.dumps(sys.executable)),
            env,
            workspace,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            guidance = client.transcript[2]["result"]["tools"][0]["inputSchema"][
                "properties"
            ]["python"]["description"]
            assert "_console.python_docs()" in guidance, guidance
            # fmt: python
            program = code("""
                import os
                import sys
                from pathlib import Path

                docs = _console.python_docs()
                assert docs["python_minor"] == f"{sys.version_info.major}.{sys.version_info.minor}"
                assert docs["python_version"] == sys.version.split()[0]
                assert (
                    docs["directory"]
                    == Path(os.environ["MCP_CONSOLE_PYTHON_DOCS"]) / docs["python_minor"] / "manual"
                )
                assert docs["download_page_release"] == docs["python_minor"] + ".99"
                assert "Python standard library" in (docs["directory"] / "library/index.txt").read_text(
                    encoding="utf-8"
                )
                print(
                    [
                        p.name
                        for p in docs["directory"].rglob("*.txt")
                        if "json.loads" in p.read_text(encoding="utf-8")
                    ]
                )
                assert (
                    (docs["directory"] / "copyright.txt")
                    .read_text()
                    .startswith("Python Software Foundation")
                )
                assert (
                    (docs["directory"] / "license.txt")
                    .read_text()
                    .startswith("Python Software Foundation")
                )
                print("active manual read")
                """)
            client.expect("['json.txt']\nactive manual read\n", python=program)
            client.expect(
                "persistent write denied\n"
                if execution == SANDBOXED
                else "host write allowed\n",
                # fmt: python
                python=code("""
                    canary = docs["directory"] / "contents.txt"
                    original = canary.read_text(encoding="utf-8")
                    try:
                        canary.write_text("overwrite", encoding="utf-8")
                    except PermissionError:
                        assert os.environ.get("MCP_CONSOLE_SANDBOX") == "1"
                        print("persistent write denied")
                    else:
                        assert os.environ.get("MCP_CONSOLE_SANDBOX") != "1"
                        canary.write_text(original, encoding="utf-8")
                        print("host write allowed")
                    """),
            )
            if execution == SANDBOXED:
                env_binary = str(binary)
                client.expect(
                    "cannot prepare Python documentation: prepare-python-docs is trusted host administration; run it outside evaluated cells\n",
                    # fmt: python
                    python=code(f"""
                        import subprocess

                        refused = subprocess.run(
                            [{json.dumps(env_binary)}, "prepare-python-docs", "--python", sys.executable],
                            capture_output=True,
                            text=True,
                        )
                        assert refused.returncode == 1
                        print(refused.stderr.strip())
                        """),
                )
                # Normalize the incidental checkout path only after exact assertions.
                client.transcript[-1]["send"]["python"] = client.transcript[-1]["send"][
                    "python"
                ].replace(env_binary, "<mcp-console binary>")
            # Lookup follows this worker's filesystem/environment, not a controller path.
            client.expect(
                "None\n42\n",
                python='os.environ["MCP_CONSOLE_PYTHON_DOCS"] += "-absent"; print(_console.python_docs()); print(42)',
            )
            client.send(control="restart")
            client.expect("['json.txt']\nactive manual read\n", python=program)
            assert (
                cache / minor / "manual/contents.txt"
            ).read_text() == "Python Documentation contents\n"
            return client.finish()


def test_manual_archive_rejects_unsafe_members_atomically(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        cases = (
            "../escape.txt",
            "/absolute.txt",
            "python-bad-docs-text/a.txt",
            f"python-{sys.version_info.major}.{sys.version_info.minor}-docs-text/../escape.txt",
            "C:/escape.txt",
            "back\\slash.txt",
            f"python-{sys.version_info.major}.{sys.version_info.minor}-docs-text/alias//",
        )
        audit = []
        for index, name in enumerate(cases):
            fixture = root / str(index)
            fixture.mkdir()
            page, archive = manual_fixture(fixture, (name, b"unsafe"))
            destination = fixture / "cache"
            result = prepare_manual(binary, destination, page, archive)
            assert result.returncode != 0, name
            assert not (
                destination / f"{sys.version_info.major}.{sys.version_info.minor}"
            ).exists(), name
            assert not list(destination.glob(".prepare-*")), name
            audit.append(
                {
                    "member": name.replace(
                        f"{sys.version_info.major}.{sys.version_info.minor}",
                        "<active minor>",
                    ),
                    "rejected": True,
                    "published": False,
                }
            )
        return audit


@requires(POSIX)
def test_manual_download_failure_and_retry_use_local_transport(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        page, archive = manual_fixture(root)
        downloader = root / "download.py"
        downloader.write_text(
            # fmt: python
            code(r"""
                import io
                import os
                import sys
                import urllib.error
                import urllib.request
                from pathlib import Path

                root = Path(os.environ["DOCS_FIXTURE"])


                class Response(io.BytesIO):
                    def __init__(self, data, url):
                        super().__init__(data)
                        self.url = url

                    def geturl(self):
                        return self.url


                class FixtureDownload:
                    def open(self, url, timeout):
                        assert timeout == 30
                        with (root / "requests.txt").open("a") as log:
                            log.write(url + "\n")
                        if os.environ["DOCS_FAILURE"] == "network":
                            raise urllib.error.URLError("fixture network unavailable")
                        source = root / (
                            "download.html" if url.endswith("download.html") else "manual.zip"
                        )
                        final_url = (
                            "https://untrusted.example/manual.zip"
                            if os.environ["DOCS_FAILURE"] == "redirect"
                            else url
                        )
                        return Response(source.read_bytes(), final_url)


                urllib.request.build_opener = lambda *handlers: FixtureDownload()
                assert sys.argv[1:3] == ["-I", "-c"]
                source = sys.argv[3]
                sys.argv = ["-c", *sys.argv[4:]]
                exec(compile(source, "<string>", "exec"), {"__name__": "__main__"})
                """),
            encoding="utf-8",
        )
        interpreter = root / "python"
        interpreter.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(downloader))} "$@"\n'
        )
        interpreter.chmod(0o755)
        audit = []
        for failure in ("network", "redirect", "none"):
            result = subprocess.run(
                [str(binary), "prepare-python-docs", "--python", str(interpreter)],
                env={
                    **os.environ,
                    "MCP_CONSOLE_PYTHON_DOCS": str(root / "cache"),
                    "DOCS_FIXTURE": str(root),
                    "DOCS_FAILURE": failure,
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
            minor = f"{sys.version_info.major}.{sys.version_info.minor}"
            destination = root / "cache" / minor
            if failure != "none":
                assert result.returncode != 0, result
                expected = (
                    "fixture network unavailable"
                    if failure == "network"
                    else "must remain on https://docs.python.org"
                )
                assert expected in result.stderr, result.stderr
                assert not destination.exists(), list(root.iterdir())
                assert not list((root / "cache").glob(".prepare-*"))
            else:
                assert result.returncode == 0, result.stderr
                receipt = json.loads(result.stdout)
                assert receipt["input"] == "official download"
                assert (
                    receipt["archive_sha256"]
                    == hashlib.sha256(archive.read_bytes()).hexdigest()
                )
                assert (
                    (destination / "manual/license.txt")
                    .read_text()
                    .startswith("Python Software Foundation")
                )
            audit.append({"transport": failure, "published": destination.exists()})
        requests = (root / "requests.txt").read_text().splitlines()
        assert len(requests) == 4, requests
        assert all(
            url.startswith(f"https://docs.python.org/{minor}/") for url in requests
        ), requests
        return audit


def test_manual_archive_limits_and_corruption_leave_no_cache(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        prefix = f"python-{minor}-docs-text/"
        audit = []
        for label in (
            "symlink",
            "case alias",
            "file size",
            "entry count",
            "expanded bytes",
            "compressed bytes",
            "CRC",
            "page link",
            "missing license",
        ):
            fixture = root / label
            fixture.mkdir()
            page, archive = manual_fixture(fixture)
            if label == "symlink":
                member = zipfile.ZipInfo(prefix + "link.txt")
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                with zipfile.ZipFile(archive, "a") as bundle:
                    bundle.writestr(member, "../../canary")
            elif label == "case alias":
                with zipfile.ZipFile(archive, "a") as bundle:
                    bundle.writestr(prefix + "library/JSON.txt", "case alias")
            elif label == "file size":
                with zipfile.ZipFile(archive, "a", zipfile.ZIP_DEFLATED) as bundle:
                    bundle.writestr(
                        prefix + "oversized.txt", b"x" * (8 * 1024 * 1024 + 1)
                    )
            elif label == "entry count":
                with zipfile.ZipFile(archive, "a") as bundle:
                    for index in range(4096):
                        bundle.writestr(prefix + f"small-{index}.txt", "")
            elif label == "expanded bytes":
                with zipfile.ZipFile(archive, "a") as bundle:
                    for index in range(17):
                        bundle.writestr(prefix + f"large-{index}.txt", "")
                # Untrusted central-directory sizes must be checked before extraction.
                data = bytearray(archive.read_bytes())
                offset = 0
                while (offset := data.find(b"PK\x01\x02", offset)) != -1:
                    name_length = struct.unpack_from("<H", data, offset + 28)[0]
                    name = data[offset + 46 : offset + 46 + name_length]
                    if b"/large-" in name:
                        struct.pack_into("<I", data, offset + 24, 8 * 1024 * 1024)
                    offset += 46 + name_length
                archive.write_bytes(data)
            elif label == "compressed bytes":
                with archive.open("ab") as output:
                    output.truncate(32 * 1024 * 1024 + 1)
            elif label == "CRC":
                with zipfile.ZipFile(archive, "a", zipfile.ZIP_STORED) as bundle:
                    bundle.writestr(prefix + "broken.txt", "checksum canary")
                archive.write_bytes(
                    archive.read_bytes().replace(b"checksum canary", b"checksum broken")
                )
            elif label == "page link":
                page.write_text(
                    page.read_text().replace("docs-text.zip", "docs-html.zip")
                )
            elif label == "missing license":
                replacement = fixture / "without-license.zip"
                with (
                    zipfile.ZipFile(archive) as source,
                    zipfile.ZipFile(replacement, "w") as target,
                ):
                    for member in source.infolist():
                        if not member.filename.endswith("license.txt"):
                            target.writestr(member, source.read(member))
                archive = replacement
            destination = fixture / "cache"
            result = prepare_manual(binary, destination, page, archive)
            assert result.returncode != 0, (label, result.stdout)
            assert not (destination / minor).exists(), label
            assert not list(destination.glob(".prepare-*")), label
            audit.append(
                {"invalid_archive": label, "rejected": True, "published": False}
            )
        return audit


def second_python() -> tuple[str, str] | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    # Capability discovery only: never install an interpreter or access the network.
    result = subprocess.run(
        [
            uv,
            "python",
            "find",
            "--offline",
            "--python-preference",
            "only-managed",
            "3.12",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    executable = result.stdout.strip()
    version = subprocess.run(
        [
            executable,
            "-I",
            "-c",
            'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")',
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    if version == f"{sys.version_info.major}.{sys.version_info.minor}":
        return None
    return executable, version


SECOND_PYTHON = second_python()


@requires(
    POSIX,
    Requirement(
        "second Python minor",
        SECOND_PYTHON is not None,
        "requires a locally installed uv-managed 3.12 distinct from the test interpreter",
    ),
)
@executions(DIRECT, SANDBOXED)
def test_manual_selection_uses_the_running_interpreter(
    binary: Path, execution: Execution
) -> Transcript:
    assert SECOND_PYTHON is not None
    executable, minor = SECOND_PYTHON
    with tempfile.TemporaryDirectory(
        prefix="console-docs-versions-", dir=Path.home()
    ) as temporary:
        root = Path(temporary).resolve()
        cache = root / "manuals"
        fixture = root / "first"
        fixture.mkdir()
        page, archive = manual_fixture(fixture)
        first = prepare_manual(binary, cache, page, archive)
        assert first.returncode == 0, first.stderr
        fixture = root / "second"
        fixture.mkdir()
        page, archive = manual_fixture(fixture, minor=minor)
        second = prepare_manual(binary, cache, page, archive, python=executable)
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["python_minor"] == minor
        tools = root / "tools"
        tools.mkdir()
        workspace = root / "workspace"
        workspace.mkdir()
        env = {
            **environment(tools),
            "MCP_CONSOLE_HOME": str(root / "console"),
            "MCP_CONSOLE_PYTHON_DOCS": str(cache),
            "EXPECTED_DOCS_MINOR": minor,
        }
        with McpClient(
            binary,
            execution.serve("-c", "python=" + json.dumps(executable)),
            env,
            workspace,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "selected interpreter manual\n",
                # fmt: python
                python=code("""
                    import os
                    import sys

                    docs = _console.python_docs()
                    assert docs["python_minor"] == os.environ["EXPECTED_DOCS_MINOR"]
                    assert docs["python_minor"] == f"{sys.version_info.major}.{sys.version_info.minor}"
                    assert docs["directory"].parent.name == docs["python_minor"]
                    print("selected interpreter manual")
                    """),
            )
            shutil.rmtree(cache / minor)
            client.send(control="restart")
            client.expect(
                "None\n42\n", python="print(_console.python_docs()); print(42)"
            )
            # The other valid manual is still present: restart must not select it.
            assert (
                cache
                / f"{sys.version_info.major}.{sys.version_info.minor}"
                / "manifest.json"
            ).is_file()
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
