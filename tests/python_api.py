from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

import mcp_console

from mcp_console import (
    MCPConsole,
    anthropic_tools,
    codex_config,
    openai_agents_server,
    openai_codex_sdk_options,
    register_chatlas,
)


def fake_mcp_modules(events, *, result=None):
    class StdioServerParameters:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            events.append(("parameters", kwargs))

    class StdioContext:
        async def __aenter__(self):
            events.append("stdio enter")
            return "read", "write"

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("stdio exit")

    def stdio_client(parameters):
        events.append(("stdio client", parameters.kwargs))
        return StdioContext()

    class ClientSession:
        def __init__(self, read, write):
            events.append(("session", read, write))

        async def __aenter__(self):
            events.append("session enter")
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            events.append("session exit")

        async def initialize(self):
            events.append("initialize")

        async def list_tools(self):
            events.append("list tools")
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="send",
                        description="Persistent mixed-language console.",
                        inputSchema={
                            "type": "object",
                            "properties": {"python": {"type": "string"}},
                        },
                    )
                ]
            )

        async def call_tool(self, name, arguments):
            events.append(("call", name, arguments))
            return result or SimpleNamespace(
                content=[SimpleNamespace(type="text", text="[1] 42\n")],
                isError=False,
            )

    mcp = types.ModuleType("mcp")
    mcp.__path__ = []
    mcp.ClientSession = ClientSession
    mcp_client = types.ModuleType("mcp.client")
    mcp_client.__path__ = []
    mcp_stdio = types.ModuleType("mcp.client.stdio")
    mcp_stdio.StdioServerParameters = StdioServerParameters
    mcp_stdio.stdio_client = stdio_client
    mcp.client = mcp_client
    mcp_client.stdio = mcp_stdio
    return {
        "mcp": mcp,
        "mcp.client": mcp_client,
        "mcp.client.stdio": mcp_stdio,
    }


class MCPConsoleTests(unittest.TestCase):
    def test_send_is_persistent_callable_and_owns_only_mcp_session(self) -> None:
        events = []

        async def exercise() -> None:
            console = MCPConsole(
                command=Path("/custom/mcp-console"),
                server_parameters={"env": {"MODE": "test"}},
            )
            async with console:
                self.assertIs(await console.connect(), console)
                output = await console(python="6 * 7", timeout_ms=25)
                self.assertEqual(output, "[1] 42\n")

        with patch.dict(sys.modules, fake_mcp_modules(events)):
            asyncio.run(exercise())

        self.assertEqual(
            events,
            [
                (
                    "parameters",
                    {
                        "command": "/custom/mcp-console",
                        "args": ["serve"],
                        "env": {"MODE": "test"},
                    },
                ),
                (
                    "stdio client",
                    {
                        "command": "/custom/mcp-console",
                        "args": ["serve"],
                        "env": {"MODE": "test"},
                    },
                ),
                "stdio enter",
                ("session", "read", "write"),
                "session enter",
                "initialize",
                "list tools",
                ("call", "send", {"timeout_ms": 25, "python": "6 * 7"}),
                "session exit",
                "stdio exit",
            ],
        )

    def test_send_requires_connection_and_surfaces_tool_errors(self) -> None:
        console = MCPConsole(command="mcp-console")
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            asyncio.run(console.send(r="stop('no')"))

        events = []
        result = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="evaluation failed")],
            isError=True,
        )

        async def exercise() -> None:
            async with MCPConsole(command="mcp-console") as connected:
                with self.assertRaisesRegex(RuntimeError, "evaluation failed"):
                    await connected.send(r="stop('no')")

        with patch.dict(sys.modules, fake_mcp_modules(events, result=result)):
            asyncio.run(exercise())

    def test_openai_responses_tool_uses_live_mcp_schema_and_returns_output_item(
        self,
    ) -> None:
        events = []

        async def exercise() -> None:
            async with MCPConsole(command="mcp-console-dev") as console:
                tool = console.openai_responses_tool()
                self.assertEqual(
                    tool.definition,
                    {
                        "type": "function",
                        "name": "send",
                        "description": "Persistent mixed-language console.",
                        "parameters": {
                            "type": "object",
                            "properties": {"python": {"type": "string"}},
                        },
                        "strict": False,
                    },
                )
                output = await tool(
                    SimpleNamespace(call_id="call_1", arguments='{"python":"6 * 7"}')
                )
                self.assertEqual(
                    output,
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "[1] 42\n",
                    },
                )

        with patch.dict(sys.modules, fake_mcp_modules(events)):
            asyncio.run(exercise())

    def test_openai_responses_tool_preserves_image_output(self) -> None:
        events = []
        result = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="image",
                    mimeType="image/png",
                    data="aW1hZ2U=",
                )
            ],
            isError=False,
        )

        async def exercise() -> None:
            async with MCPConsole(command="mcp-console-dev") as console:
                tool = console.openai_responses_tool()
                output = await tool.call({"python": "plot()"})
                self.assertEqual(
                    output,
                    [
                        {
                            "type": "input_image",
                            "detail": "auto",
                            "image_url": "data:image/png;base64,aW1hZ2U=",
                        }
                    ],
                )

        with patch.dict(sys.modules, fake_mcp_modules(events, result=result)):
            asyncio.run(exercise())

    def test_callable_tool_adapters_return_framework_native_objects(self) -> None:
        calls = []

        agents = types.ModuleType("agents")

        def function_tool(func, **kwargs):
            calls.append(("openai", func, kwargs))
            return "openai-send"

        agents.function_tool = function_tool

        anthropic = types.ModuleType("anthropic")

        def beta_async_tool(func, **kwargs):
            calls.append(("anthropic", func, kwargs))
            return "anthropic-send"

        anthropic.beta_async_tool = beta_async_tool

        console = MCPConsole(command="mcp-console")
        with patch.dict(sys.modules, {"agents": agents, "anthropic": anthropic}):
            self.assertEqual(
                console.openai_agents_tool(strict_mode=False), "openai-send"
            )
            self.assertEqual(console.anthropic_tool(strict=True), "anthropic-send")

        self.assertEqual(calls[0][0], "openai")
        self.assertEqual(calls[0][1].__self__, console)
        self.assertEqual(calls[0][1].__name__, "send")
        self.assertEqual(calls[0][2], {"strict_mode": False})
        self.assertEqual(calls[1][0], "anthropic")
        self.assertEqual(calls[1][1].__self__, console)
        self.assertEqual(calls[1][1].__name__, "send")
        self.assertEqual(calls[1][2], {"strict": True})


class NativeFrameworkTests(unittest.TestCase):
    def test_register_chatlas_delegates_to_existing_chat(self) -> None:
        calls = []

        class Chat:
            async def register_mcp_tools_stdio_async(self, **kwargs):
                calls.append(kwargs)
                return "registered"

        result = asyncio.run(
            register_chatlas(
                Chat(),
                command=Path("/custom/mcp-console"),
                args=["serve", "--future-option"],
                name="console",
                namespace="analysis",
            )
        )

        self.assertEqual(result, "registered")
        self.assertEqual(
            calls,
            [
                {
                    "command": "/custom/mcp-console",
                    "args": ["serve", "--future-option"],
                    "name": "console",
                    "namespace": "analysis",
                }
            ],
        )

    def test_openai_agents_server_returns_native_mcp_server(self) -> None:
        calls = []

        class MCPServerStdio:
            def __init__(self, **kwargs):
                calls.append(kwargs)

        agents = types.ModuleType("agents")
        agents.__path__ = []
        agents_mcp = types.ModuleType("agents.mcp")
        agents_mcp.MCPServerStdio = MCPServerStdio
        agents.mcp = agents_mcp

        with patch.dict(
            sys.modules,
            {"agents": agents, "agents.mcp": agents_mcp},
        ):
            server = openai_agents_server(
                command="mcp-console-dev",
                params={"env": {"MODE": "test"}},
                cache_tools_list=True,
            )

        self.assertIsInstance(server, MCPServerStdio)
        self.assertEqual(
            calls,
            [
                {
                    "name": "MCP Console",
                    "params": {
                        "command": "mcp-console-dev",
                        "args": ["serve"],
                        "env": {"MODE": "test"},
                    },
                    "cache_tools_list": True,
                }
            ],
        )

    def test_anthropic_tools_yields_native_converted_tools(self) -> None:
        events = []
        modules = fake_mcp_modules(events)

        def async_mcp_tool(tool, client, **kwargs):
            events.append(("convert", tool.name, client, kwargs))
            return "anthropic-send"

        anthropic = types.ModuleType("anthropic")
        anthropic.__path__ = []
        anthropic_lib = types.ModuleType("anthropic.lib")
        anthropic_lib.__path__ = []
        anthropic_tools_module = types.ModuleType("anthropic.lib.tools")
        anthropic_tools_module.__path__ = []
        anthropic_mcp = types.ModuleType("anthropic.lib.tools.mcp")
        anthropic_mcp.async_mcp_tool = async_mcp_tool
        anthropic.lib = anthropic_lib
        anthropic_lib.tools = anthropic_tools_module
        anthropic_tools_module.mcp = anthropic_mcp
        modules.update(
            {
                "anthropic": anthropic,
                "anthropic.lib": anthropic_lib,
                "anthropic.lib.tools": anthropic_tools_module,
                "anthropic.lib.tools.mcp": anthropic_mcp,
            }
        )

        async def exercise() -> None:
            async with anthropic_tools(
                command="mcp-console-dev",
                tool_kwargs={"strict": True},
            ) as tools:
                self.assertEqual(tools, ["anthropic-send"])
                events.append("body")

        with patch.dict(sys.modules, modules):
            asyncio.run(exercise())

        self.assertIn(("convert", "send", unittest.mock.ANY, {"strict": True}), events)
        self.assertLess(events.index("session enter"), events.index("body"))
        self.assertLess(events.index("body"), events.index("session exit"))


class CodexTests(unittest.TestCase):
    def test_codex_config_is_plain_thread_configuration(self) -> None:
        config = codex_config(
            command="/custom/mcp-console",
            args=["serve"],
            server_name="console",
            config={
                "model_reasoning_effort": "high",
                "mcp_servers": {"existing": {"command": "other"}},
            },
            server_parameters={"startup_timeout_sec": 30},
        )

        self.assertEqual(
            config,
            {
                "model_reasoning_effort": "high",
                "mcp_servers": {
                    "existing": {"command": "other"},
                    "console": {
                        "command": "/custom/mcp-console",
                        "args": ["serve"],
                        "startup_timeout_sec": 30,
                    },
                },
            },
        )

    def test_legacy_codex_sdk_helper_yields_options_without_creating_codex(
        self,
    ) -> None:
        package = types.ModuleType("openai_codex_sdk")
        package.__path__ = []
        exec_module = types.ModuleType("openai_codex_sdk.exec")
        exec_module.find_codex_path = lambda: "/opt/codex binary"
        package.exec = exec_module

        modules = {
            "openai_codex_sdk": package,
            "openai_codex_sdk.exec": exec_module,
        }

        with patch.dict(sys.modules, modules):
            with openai_codex_sdk_options(
                command="/custom/mcp console",
                args=["serve", "--future-option"],
                server_name="analysis console",
                options={"api_key": "test"},
            ) as options:
                self.assertEqual(options["api_key"], "test")
                launcher = Path(options["codex_path_override"])
                self.assertTrue(launcher.is_file())
                source = launcher.read_text(encoding="utf-8")
                self.assertIn('mcp_servers."analysis console".command', source)
                self.assertIn("/custom/mcp console", source)
                self.assertIn("exec '/opt/codex binary'", source)

        self.assertFalse(launcher.exists())


class PackagingTests(unittest.TestCase):
    def test_package_does_not_export_framework_chat_objects(self) -> None:
        self.assertFalse(hasattr(mcp_console, "Codex"))
        self.assertFalse(hasattr(mcp_console, "Agent"))
        self.assertFalse(hasattr(mcp_console, "AsyncOpenAI"))

    def test_optional_dependencies_keep_frameworks_separate(self) -> None:
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('codex = ["openai-codex"]', project)
        self.assertIn('codex-sdk = ["openai-codex-sdk>=0.1.11"]', project)
        self.assertIn('openai-agents = ["openai-agents"]', project)

    def test_python_sources_parse_with_python_38_grammar(self) -> None:
        import ast

        for source in (ROOT / "python" / "mcp_console").glob("*.py"):
            ast.parse(
                source.read_text(encoding="utf-8"),
                filename=str(source),
                feature_version=(3, 8),
            )


if __name__ == "__main__":
    unittest.main()
