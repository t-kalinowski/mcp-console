import codecs
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, Self

from ._common import Command, Requirements, result_text, stdio_command

if TYPE_CHECKING:
    from mcp.types import CallToolResult, Tool


class MCPConsole:
    """A synchronous, persistent connection to ``mcp-console serve``.

    Use as a context manager or explicitly call ``connect()`` and ``close()``.
    The context starts Console and discovers its tools; leaving it closes the
    connection and subprocess. Variables persist across calls on this connection.
    The application owns its model clients, agents, and tool loops.

    Calls run on the calling thread. Use one request at a time per connection;
    overlapping calls raise `RuntimeError`. Catching `KeyboardInterrupt` while
    waiting cancels that request's wait, leaving an admitted cell running.
    Poll to collect its output or send `control="interrupt"` to stop execution.
    Connect and close only when no other call is in progress.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        server_parameters: MCP stdio settings such as `cwd` and `env`.

    Examples:
        Install with `pip install 'mcp-console[client]'`. Define values in one
        cell and use them in the next:

        ```python
        from mcp_console import MCPConsole

        with MCPConsole() as console:
            print(console.send(python="values = [12, 15, 18, 20, 25]; values"))
            print(
                console.send(
                    python="import statistics; statistics.mean(values)"
                )
            )
        ```

        Create framework adapters inside this context, after discovery. See
        [chatlas](chatlas.tool.html), [Anthropic](anthropic.tool.html),
        [Responses](openai.responses_tool.html), or [Agents](openai.agents_tool.html).
    """

    def __init__(
        self,
        *,
        command: Command | None = None,
        args: Sequence[Command] | None = None,
        server_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._server_parameters = dict(server_parameters or {})
        self._process: subprocess.Popen[bytes] | None = None
        self._send_tool: Tool | None = None
        self._request_lock = Lock()
        self._next_id = 0
        self._abandoned: set[int] = set()
        self._buffer = ""
        self._encoding = "utf-8"
        self._encoding_errors = "strict"
        self._decoder = codecs.getincrementaldecoder(self._encoding)()

    def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._process is not None:
            return self

        from mcp import StdioServerParameters
        from mcp.client.stdio import get_default_environment
        from mcp.types import InitializeResult, ListToolsResult
        from mcp.types.version import (
            HANDSHAKE_PROTOCOL_VERSIONS,
            LATEST_HANDSHAKE_VERSION,
        )

        command, args = stdio_command(self._command, self._args)
        parameters = StdioServerParameters(
            **(self._server_parameters | {"command": command, "args": args})
        )
        self._encoding = parameters.encoding
        self._encoding_errors = parameters.encoding_error_handler
        self._decoder = codecs.getincrementaldecoder(self._encoding)(
            errors=self._encoding_errors
        )
        self._buffer = ""
        self._abandoned.clear()
        self._next_id = 0
        self._process = subprocess.Popen(
            [parameters.command, *parameters.args],
            cwd=parameters.cwd,
            env=get_default_environment() | (parameters.env or {}),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            # Console uses the initialized MCP lifecycle, as the async client does.
            initialized = InitializeResult.model_validate(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": LATEST_HANDSHAKE_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "mcp-console-python", "version": "1.0"},
                    },
                )
            )
            if initialized.protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
                raise RuntimeError(
                    f"Unsupported protocol version from the server: {initialized.protocol_version}"
                )
            self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
            tools = ListToolsResult.model_validate(self._request("tools/list", {}))
            self._send_tool = next(
                (tool for tool in tools.tools if tool.name == "send"), None
            )
            if self._send_tool is None:
                raise RuntimeError("MCP Console did not expose its send tool")
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Close the server's input, drain its output, and reap its process."""
        process, self._process = self._process, None
        self._send_tool = None
        self._buffer = ""
        self._abandoned.clear()
        if process is not None:
            assert process.stdin is not None and process.stdout is not None
            with process.stdin, process.stdout:
                try:
                    # Closing input requests owned cleanup. Drain concurrently
                    # so a final response cannot block the server's shutdown.
                    process.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def __enter__(self) -> "Self":
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        from mcp import MCPError
        from mcp.types import ErrorData

        if self._process is None:
            raise RuntimeError(
                "MCPConsole is not connected; use `with MCPConsole() as console` "
                "or call `console.connect()` first"
            )
        if not self._request_lock.acquire(blocking=False):
            raise RuntimeError("MCPConsole already has a pending request")
        settled = False
        try:
            self._next_id += 1
            request_id = self._next_id
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                }
            )
            try:
                response = self._read(request_id)
            except KeyboardInterrupt:
                # An interrupted write cannot be resumed safely. Only cancel
                # fully written requests; initialize must never be cancelled.
                if method != "initialize":
                    self._abandoned.add(request_id)
                    self._write(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/cancelled",
                            "params": {
                                "requestId": request_id,
                                "reason": "User interrupted the request.",
                            },
                        }
                    )
                    settled = True
                raise
            settled = True
        finally:
            try:
                if not settled:
                    self.close()
            finally:
                self._request_lock.release()
        if "error" in response:
            raise MCPError.from_error_data(ErrorData.model_validate(response["error"]))
        return response["result"]

    def _write(self, message: Mapping[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        remaining = memoryview(
            (json.dumps(message, ensure_ascii=False) + "\n").encode(
                self._encoding, errors=self._encoding_errors
            )
        )
        while remaining:
            written = self._process.stdin.write(remaining)
            assert written is not None and written > 0
            remaining = remaining[written:]

    def _read(self, request_id: int) -> dict[str, Any]:
        assert self._process is not None and self._process.stdout is not None
        while True:
            # Keep incomplete frames across KeyboardInterrupt. A buffered text
            # readline can lose a partial line when its blocking read is interrupted.
            while "\n" not in self._buffer:
                chunk = self._process.stdout.read(65536)
                if not chunk:
                    raise RuntimeError("MCP Console closed its output before replying")
                self._buffer += self._decoder.decode(chunk)
            line, self._buffer = self._buffer.split("\n", 1)
            response = json.loads(line)
            if response.get("jsonrpc") != "2.0":
                raise RuntimeError("MCP Console returned an invalid JSON-RPC message")
            if "method" in response and "id" not in response:
                # Notifications have no result in this request/response API.
                continue
            response_id = response.get("id")
            if response_id in self._abandoned:
                self._abandoned.remove(response_id)
                continue
            if response_id != request_id or not (
                "result" in response or "error" in response
            ):
                raise RuntimeError(
                    "MCP Console returned an unexpected JSON-RPC message"
                )
            return response

    def _call_send(self, arguments: Mapping[str, Any]) -> "CallToolResult":
        from mcp.types import CallToolResult

        return CallToolResult.model_validate(
            self._request("tools/call", {"name": "send", "arguments": dict(arguments)})
        )

    def send(
        self,
        *,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Run or control a cell on this connection and return its text output.

        The arguments and polling behavior match
        [AsyncMCPConsole.send()](AsyncMCPConsole.html#send). Send one language
        per call. If output ends in `[running; poll with an empty send]`, collect
        the remaining output with `console.send()` before submitting another cell.
        Images appear as MIME placeholders; MCP tool errors raise `RuntimeError`.
        """
        arguments = {
            "r": r,
            "python": python,
            "sql": sql,
            "control": control,
            "requirements": requirements,
            "stdin": stdin,
            "timeout_ms": timeout_ms,
        }
        return result_text(
            self._call_send(
                {name: value for name, value in arguments.items() if value is not None}
            )
        )

    __call__ = send

    @property
    def send_tool(self) -> "Tool":
        """The connected server's MCP tool definition, used by SDK adapters.

        Available after connecting. Its input schema reflects the server's
        configured capabilities. Use a framework adapter to register this
        schema; recreate adapters after reconnecting.
        """
        if self._send_tool is None:
            raise RuntimeError(
                "Connect the console before creating tools; enter its context or call connect()"
            )
        return self._send_tool
