import base64
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent
from typing import Any

TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]
ToolResult = dict[str, Any]
YamlStream = list[Any]


@dataclass(frozen=True)
class TranscriptWithCompanions:
    transcript: Transcript
    companions: dict[str, YamlStream | str]


class FifoCheckpoint:
    def __init__(self, path: Path) -> None:
        self.path = path
        os.mkfifo(path)
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.descriptor)

    def wait(self, description: str, timeout: float = 10) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], timeout)
        assert readable, f"checkpoint was not reached: {description}"
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        assert os.write(self.descriptor, b"1") == 1


def checkpoint_uv_environment(
    temporary: Path,
    argument: str,
) -> tuple[dict[str, str], FifoCheckpoint, FifoCheckpoint]:
    real_uv = shutil.which("uv")
    assert real_uv is not None, "real uv is required"
    started = FifoCheckpoint(temporary / "uv-started")
    release = FifoCheckpoint(temporary / "uv-release")
    environment = os.environ.copy()
    environment["RETICULATE_UV"] = str(
        Path(__file__).parent.parent / "fixtures" / "checkpoint_uv"
    )
    environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_ARGUMENT"] = argument
    environment["MCP_CONSOLE_TEST_UV_CHECKPOINT_CLAIM"] = str(temporary / "uv-claimed")
    environment["MCP_CONSOLE_TEST_UV_STARTED"] = str(started.path)
    environment["MCP_CONSOLE_TEST_UV_RELEASE"] = str(release.path)
    return environment, started, release


def code(source: str) -> str:
    return dedent(source).removeprefix("\n")


def normalize_python_resolution_error(error: str, invalid: str | None = None) -> str:
    error = normalize_python_traceback_paths(error)
    error, python_patch = re.subn(
        r'(?m)^(  "python": "\d+\.\d+)\.\d+( \(reticulate default\))?(",)$',
        r"\1.x\2\3",
        error,
        count=1,
    )
    assert python_patch == 1, error
    has_python_version = '\n  "python_version": [\n' in error
    error, python_version_patch = re.subn(
        r'(?m)^(  "python_version": \[\n    "\d+\.\d+)\.\d+("\n  \])$',
        r"\1.x\2",
        error,
        count=1,
    )
    assert python_version_patch == int(has_python_version), error
    if invalid is not None:
        assert invalid in error, error
    return error


def normalize_python_traceback_paths(error: str) -> str:
    replacements = (
        (
            r'(?m)^(\s+File ")[^"\n]*/reticulate/python/(rpytools/loader\.py")',
            r"\1<reticulate>/python/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/lib/python\d+\.\d+/(importlib/__init__\.py")',
            r"\1<python-stdlib>/\2",
        ),
        (
            r'(?m)^(\s+File ")[^"\n]*/(tests/fixtures/checkpoint_uv")',
            r"\1<workspace>/\2",
        ),
    )
    for pattern, replacement in replacements:
        error = re.sub(pattern, replacement, error)
    assert re.search(r'(?m)^\s+File "/', error) is None, error
    return error


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


def build_r_input_handler(
    directory: Path,
    environment: dict[str, str],
    rscript: Path,
) -> None:
    source = Path(__file__).parent.parent / "fixtures" / "r_input_handler.c"
    local_source = directory / source.name
    shutil.copyfile(source, local_source)
    subprocess.run(
        [
            rscript.parent / "R",
            "CMD",
            "SHLIB",
            "-o",
            "mcp_test_input_handler.so",
            local_source.name,
        ],
        cwd=directory,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


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


def release_worker_callback_gate(
    client: "McpClient",
    description: str,
    extra_path_labels: tuple[str, ...] = (),
) -> tuple[Path, ...]:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    paths = content[0]["text"].splitlines()
    assert len(paths) == 2 + len(extra_path_labels), content
    content[0]["text"] = "\n".join(
        (
            "<worker callback gate>",
            "<worker callback checkpoint>",
            *(f"<worker callback {label}>" for label in extra_path_labels),
        )
    )

    gate, checkpoint, *extra_paths = map(Path, paths)
    gate.touch()
    deadline = time.monotonic() + 5
    while not checkpoint.exists():
        assert client.process.poll() is None, (
            f"mcp-console stopped before {description} reached its checkpoint"
        )
        assert time.monotonic() < deadline, (
            f"{description} did not reach its checkpoint"
        )
        time.sleep(0.01)
    return tuple(extra_paths)


def wait_for_idle_output(
    client: "McpClient",
    expected: str,
    description: str,
    **send_arguments: Any,
) -> None:
    """Poll the public idle snapshot until a worker event reaches the server."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    while True:
        result = client.send(**send_arguments)
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == "\n[idle]", output
        if time.monotonic() >= deadline:
            raise AssertionError(f"{description} did not reach the server")
        time.sleep(0.01)

    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]


def wait_for_evaluation_output(
    client: "McpClient",
    expected: str,
    description: str,
    **send_arguments: Any,
) -> None:
    """Poll past a provisional input request and retain the submitted call."""
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    result = client.send(**send_arguments)
    while True:
        assert result.get("isError") is not True, result
        content = result["content"]
        assert len(content) == 1 and content[0]["type"] == "text", content
        output = content[0]["text"]
        if output == expected:
            break
        assert output == "\n[waiting for stdin]", repr(output)
        assert time.monotonic() < deadline, f"{description} did not complete"
        result = client.send(timeout_ms=3_000)

    calls = client.transcript[poll_start:]
    submitted = calls[0]
    submitted["result"] = calls[-1]["result"]
    client.transcript[poll_start:] = [submitted]


def run_this_suite(suite_path: str) -> None:
    suite = Path(suite_path).resolve()
    directory = next(
        parent for parent in suite.parents if (parent / "_run.py").is_file()
    )
    root = directory.parents[1]
    suite_name = suite.relative_to(directory).with_suffix("").as_posix()
    subprocess.run([root / "scripts" / "test", suite_name], check=True)


class McpClient:
    def __init__(
        self,
        binary: Path,
        arguments: tuple[str, ...] = (),
        environment: dict[str, str] | None = None,
        current_directory: Path | None = None,
        umask: int = -1,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
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
            pass_fds=pass_fds,
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
        self._issued_request_ids: set[int] = set()

    def send(self, **arguments: Any) -> ToolResult:
        return self._call_tool("send", **arguments)

    def _send_message(self, message: dict[str, Any]) -> TranscriptEntry:
        recorded_message = message.copy()
        assert recorded_message.pop("jsonrpc", None) == "2.0", message

        entry = {}
        if "id" in recorded_message:
            request_id = recorded_message.pop("id")
            assert isinstance(request_id, int), message
            assert request_id not in self._issued_request_ids, (
                f"JSON-RPC request ID was reused: {request_id}"
            )
            self._issued_request_ids.add(request_id)
            entry["id"] = request_id
        params = recorded_message.get("params")
        if (
            recorded_message.keys() == {"method", "params"}
            and recorded_message["method"] == "tools/call"
            and isinstance(params, dict)
            and params.keys() == {"name", "arguments"}
            and params["name"] == "send"
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
        assert len(pending) == len(entries), "response batch reused a request ID"
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


def wait_for_worker_file(root: Path, name: str, client: McpClient) -> Path:
    deadline = time.monotonic() + 10
    while True:
        paths = list(root.glob(f"**/{name}"))
        if paths:
            assert len(paths) == 1, paths
            return paths[0]
        assert client.process.poll() is None, (
            "mcp-console stopped before worker checkpoint"
        )
        assert time.monotonic() < deadline, f"worker did not create {name}"
        time.sleep(0.01)


def stop_client(client: McpClient) -> None:
    if client.process.poll() is not None:
        return
    if not client.stdin.closed:
        client.stdin.close()
    try:
        client.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        client.process.kill()
        client.process.wait()
