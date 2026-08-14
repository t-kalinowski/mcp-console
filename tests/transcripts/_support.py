import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any


TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]
ToolResult = dict[str, Any]
YamlStream = list[Any]


def host_ir_cache_dir() -> str | None:
    if cache := os.environ.get("IR_CACHE_DIR"):
        return cache
    ir = shutil.which("ir")
    if ir is None:
        return None
    result = subprocess.run(
        [ir, "cache", "dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    cache = result.stdout.strip()
    return cache or None


HOST_IR_CACHE_DIR = host_ir_cache_dir()


@dataclass(frozen=True)
class TranscriptWithCompanion:
    transcript: Transcript
    companion_name: str
    companion: YamlStream


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def normalize_python_resolution_error(error: str, invalid: str | None = None) -> str:
    error, python_patch = re.subn(
        r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\)",)$',
        r"\1.x\2",
        error,
        count=1,
    )
    assert python_patch == 1, error
    if invalid is not None:
        error, uv_indentation = re.subn(
            rf"(?m)^(?P<indent> *)({re.escape(invalid)})\n(?P=indent)(?P<caret> +\^)$",
            lambda match: f"{match.group(2)}\n{match.group('caret')}",
            error,
        )
        assert uv_indentation == 1, error
    return "\n".join(line.rstrip() for line in error.splitlines())


def r_test_environment() -> tuple[dict[str, str], Path]:
    environment = os.environ.copy()
    if r_home := environment.get("R_HOME"):
        home = Path(r_home)
    else:
        output = subprocess.run(
            ["R", "RHOME"],
            check=True,
            capture_output=True,
            text=True,
        )
        home = Path(output.stdout.strip())
        environment["R_HOME"] = str(home)
    return environment, home / "bin" / "Rscript"


def use_temporary_home(environment: dict[str, str], home: Path) -> None:
    original_home = Path(environment.get("HOME", str(Path.home())))
    environment.setdefault(
        "RENV_PATHS_CACHE",
        str(original_home / "Library/Caches/org.R-project.R/R/renv/cache"),
    )
    # Keep IR resolution records and their package links under the same
    # lifetime while retaining the host's stable package cache.
    environment["IR_CACHE_DIR"] = str(home.parent / "ir-cache")
    environment["HOME"] = str(home)


def reference_plots(
    rscript: Path,
    environment: dict[str, str],
    source: str,
    *,
    width: float,
    height: float,
    dpi: float,
    pages: int,
    expected_error: str | None = None,
) -> list[bytes]:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        error_handler = ""
        if expected_error is not None:
            message = json.dumps(expected_error)
            error_handler = (
                ", error = function(error) "
                f"stopifnot(identical(conditionMessage(error), {message}))"
            )
        script = (
            "base::local({\n"
            "  directory <- commandArgs(trailingOnly = TRUE)[[1L]]\n"
            "  device_counter <- 0L\n"
            "  options(device = function(...) {\n"
            "    device_counter <<- device_counter + 1L\n"
            "    grDevices::png(\n"
            "      filename = file.path(\n"
            "        directory,\n"
            '        sprintf("device-%06d-page-%%06d.png", device_counter)\n'
            "      ),\n"
            f'      width = {width}, height = {height}, units = "in", res = {dpi}\n'
            "    )\n"
            "  })\n"
            "  tryCatch({\n"
            f"{source}"
            f"  }}{error_handler}, finally = grDevices::graphics.off())\n"
            "})\n"
        )
        subprocess.run(
            [rscript, "--vanilla", "-", str(directory)],
            input=script,
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        paths = sorted(directory.glob("device-*-page-*.png"))
        assert len(paths) == pages, paths
        return [path.read_bytes() for path in paths]


def assert_result_content(
    client: "McpClient",
    expected: list[str | bytes],
    *,
    image_reference: str = "live Rscript page {page}",
) -> None:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == len(expected), (
        f"expected {len(expected)} content blocks, got "
        f"{[item.get('type') for item in content]}"
    )
    page = 0
    for item, expected_item in zip(content, expected):
        if isinstance(expected_item, str):
            assert item == {"type": "text", "text": expected_item}, item
            continue

        image = item
        assert image.keys() == {"type", "data", "mimeType"}, image
        assert image["type"] == "image", image
        assert image["mimeType"] == "image/png", image
        data = base64.b64decode(image["data"], validate=True)
        reference = image_reference.format(page=page + 1)
        assert data == expected_item, (
            f"plot bytes differ: worker returned {len(data)} bytes, "
            f"{reference} returned {len(expected_item)} bytes"
        )
        page += 1
        image["data"] = f"<PNG byte-identical to {reference}>"


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    root = suite.parents[2]
    subprocess.run([root / "scripts" / "test", suite.stem], check=True)


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        current_directory: Path | None = None,
        umask: int = -1,
    ) -> None:
        if environment is not None:
            environment = environment.copy()
            if HOST_IR_CACHE_DIR is not None:
                environment.setdefault("IR_CACHE_DIR", HOST_IR_CACHE_DIR)
        self.temporary_directory = (
            tempfile.TemporaryDirectory() if current_directory is None else None
        )
        if current_directory is None:
            assert self.temporary_directory is not None
            current_directory = Path(self.temporary_directory.name)
        process = subprocess.Popen(
            [binary, *arguments],
            env=environment,
            cwd=current_directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            umask=umask,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        self.process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self.transcript: Transcript = []
        self._next_request_id = 1

    def send(self, **arguments: Any) -> ToolResult:
        return self._call_tool("send", **arguments)

    def session(self, **arguments: Any) -> ToolResult:
        return self._call_tool("session", **arguments)

    def _send_message(self, message: dict[str, Any]) -> TranscriptEntry:
        recorded_message = message.copy()
        assert recorded_message.pop("jsonrpc", None) == "2.0", message

        entry = {}
        if "id" in recorded_message:
            entry["id"] = recorded_message.pop("id")
        params = recorded_message.get("params")
        if (
            recorded_message.keys() == {"method", "params"}
            and recorded_message["method"] == "tools/call"
            and isinstance(params, dict)
            and params.keys() == {"name", "arguments"}
            and params["name"] in {"send", "session"}
            and isinstance(params["arguments"], dict)
        ):
            entry[params["name"]] = params["arguments"]
        else:
            entry["input"] = recorded_message
        self.transcript.append(entry)
        self.stdin.write(json.dumps(message) + "\n")
        self.stdin.flush()
        return entry

    def _receive(self, entry: TranscriptEntry) -> None:
        line = self.stdout.readline()
        assert line, "mcp-console stopped before replying"
        message = json.loads(line)
        assert message.pop("jsonrpc", None) == "2.0", message
        assert message.pop("id", None) == entry["id"], message
        assert message.keys() == {"result"} or message.keys() == {"error"}, message
        assert entry.keys().isdisjoint(message), message
        entry.update(message)

    def _receive_many(self, entries: list[TranscriptEntry]) -> None:
        pending = {entry["id"]: entry for entry in entries}
        for _ in entries:
            line = self.stdout.readline()
            assert line, "mcp-console stopped before replying"
            message = json.loads(line)
            assert message.pop("jsonrpc", None) == "2.0", message
            request_id = message.pop("id", None)
            assert request_id in pending, message
            entry = pending.pop(request_id)
            assert message.keys() == {"result"} or message.keys() == {"error"}, message
            assert entry.keys().isdisjoint(message), message
            entry.update(message)

    def _start_request(self, method: str, **params: Any) -> TranscriptEntry:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._next_request_id,
            "method": method,
        }
        self._next_request_id += 1
        if params:
            message["params"] = params

        return self._send_message(message)

    def _request(self, method: str, **params: Any) -> TranscriptEntry:
        entry = self._start_request(method, **params)
        self._receive(entry)
        return entry

    def _notify(self, method: str, **params: Any) -> None:
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params:
            message["params"] = params

        self._send_message(message)

    def _initialize_and_list_tools(self) -> None:
        self._request(
            "initialize",
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo={
                "name": "acceptance-test",
                "version": "1.0.0",
            },
        )
        self._notify("notifications/initialized")
        self._request("tools/list")

    def _start_tool_call(self, name: str, **arguments: Any) -> TranscriptEntry:
        return self._start_request(
            "tools/call",
            name=name,
            arguments=arguments,
        )

    def _call_tool(self, name: str, **arguments: Any) -> ToolResult:
        entry = self._start_tool_call(name, **arguments)
        self._receive(entry)
        result = entry["result"]
        assert isinstance(result, dict), result
        return result

    def _start_send(self, **arguments: Any) -> TranscriptEntry:
        return self._start_tool_call("send", **arguments)

    def _start_session(self, **arguments: Any) -> TranscriptEntry:
        return self._start_tool_call("session", **arguments)

    def _finish(self) -> Transcript:
        transcript, standard_error = self._finish_with_standard_error()
        assert standard_error == "", standard_error
        return transcript

    def _finish_with_standard_error(self) -> tuple[Transcript, str]:
        self.stdin.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            stdout = executor.submit(self.stdout.read)
            stderr = executor.submit(self.stderr.read)
            return_code = self.process.wait()
            extra_output = stdout.result()
            standard_error = stderr.result()

        assert return_code == 0, standard_error
        assert extra_output == "", f"unexpected extra output: {extra_output}"
        return self.transcript, standard_error
