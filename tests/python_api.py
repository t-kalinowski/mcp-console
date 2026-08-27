from __future__ import annotations

import asyncio
import os
import subprocess
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


class ChatlasTests(unittest.TestCase):
    def test_register_chatlas_delegates_to_native_stdio_registration(self) -> None:
        calls = []

        class Chat:
            async def register_mcp_tools_stdio_async(self, **kwargs):
                calls.append(kwargs)
                return "registered"

        result = asyncio.run(
            mcp_console.register_chatlas(
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

    def test_default_command_prefers_current_environment_scripts_directory(
        self,
    ) -> None:
        calls = []

        class Chat:
            async def register_mcp_tools_stdio_async(self, **kwargs):
                calls.append(kwargs)

        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "mcp-console"
            executable.write_text("", encoding="utf-8")
            executable.chmod(0o755)
            with patch.object(
                mcp_console.sysconfig, "get_path", return_value=directory
            ):
                with patch.object(mcp_console.shutil, "which", return_value=None):
                    asyncio.run(mcp_console.register_chatlas(Chat()))

        self.assertEqual(
            calls,
            [{"command": str(executable), "args": ["serve"]}],
        )


class OpenAITests(unittest.TestCase):
    def test_openai_agents_server_delegates_to_mcp_server_stdio(self) -> None:
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
            server = mcp_console.openai_agents_server(
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


class OpenAICodexTests(unittest.TestCase):
    def test_openai_codex_injects_mcp_overrides_and_delegates(self) -> None:
        calls = []

        class Codex:
            def __init__(self, options):
                self.options = options
                calls.append(options)

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            captured = directory_path / "arguments.txt"
            executable = directory_path / "real codex"
            executable.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n',
                encoding="utf-8",
            )
            executable.chmod(0o755)

            sdk = types.ModuleType("openai_codex_sdk")
            sdk.__path__ = []
            sdk.Codex = Codex
            sdk_exec = types.ModuleType("openai_codex_sdk.exec")
            sdk_exec.find_codex_path = lambda: str(executable)
            sdk.exec = sdk_exec

            with patch.dict(
                sys.modules,
                {
                    "openai_codex_sdk": sdk,
                    "openai_codex_sdk.exec": sdk_exec,
                },
            ):
                with mcp_console.openai_codex(
                    command=Path("/custom/mcp-console"),
                    args=["serve", "--future-option"],
                    server_name="analysis console",
                    options={"baseUrl": "https://example.test"},
                ) as codex:
                    launcher = Path(codex.options["codex_path_override"])
                    self.assertTrue(launcher.is_file())
                    environment = os.environ.copy()
                    environment["CAPTURE"] = str(captured)
                    subprocess.run(
                        [str(launcher), "exec", "--experimental-json"],
                        env=environment,
                        check=True,
                    )

                self.assertFalse(launcher.exists())
                captured_arguments = captured.read_text(encoding="utf-8").splitlines()

        self.assertEqual(
            captured_arguments,
            [
                "exec",
                "--config",
                'mcp_servers."analysis console".command="/custom/mcp-console"',
                "--config",
                'mcp_servers."analysis console".args=["serve", "--future-option"]',
                "--experimental-json",
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["baseUrl"], "https://example.test")
        self.assertEqual(calls[0]["codex_path_override"], str(launcher))

    def test_openai_codex_accepts_the_sdk_camel_case_path_option(self) -> None:
        class Codex:
            def __init__(self, options):
                self.options = options

        sdk = types.ModuleType("openai_codex_sdk")
        sdk.__path__ = []
        sdk.Codex = Codex
        sdk_exec = types.ModuleType("openai_codex_sdk.exec")
        sdk_exec.find_codex_path = lambda: self.fail("resolver should not run")
        sdk.exec = sdk_exec

        with patch.dict(
            sys.modules,
            {
                "openai_codex_sdk": sdk,
                "openai_codex_sdk.exec": sdk_exec,
            },
        ):
            with mcp_console.openai_codex(
                command="mcp-console-dev",
                options={"codexPathOverride": "/custom/codex"},
            ) as codex:
                source = Path(codex.options["codex_path_override"]).read_text()

        self.assertIn("exec /custom/codex ", source)
        self.assertNotIn("codexPathOverride", codex.options)


class AnthropicTests(unittest.TestCase):
    def test_anthropic_tools_owns_session_and_delegates_conversion(self) -> None:
        events = []

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
                return SimpleNamespace(tools=["send"])

        def async_mcp_tool(tool, client, **kwargs):
            events.append(("convert", tool, client, kwargs))
            return "anthropic-send"

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

        modules = {
            "mcp": mcp,
            "mcp.client": mcp_client,
            "mcp.client.stdio": mcp_stdio,
            "anthropic": anthropic,
            "anthropic.lib": anthropic_lib,
            "anthropic.lib.tools": anthropic_tools_module,
            "anthropic.lib.tools.mcp": anthropic_mcp,
        }

        async def exercise() -> None:
            async with mcp_console.anthropic_tools(
                command="mcp-console-dev",
                server_parameters={"env": {"MODE": "test"}},
                tool_kwargs={"strict": True},
            ) as tools:
                self.assertEqual(tools, ["anthropic-send"])
                events.append("body")

        with patch.dict(sys.modules, modules):
            asyncio.run(exercise())

        self.assertEqual(
            events,
            [
                (
                    "parameters",
                    {
                        "command": "mcp-console-dev",
                        "args": ["serve"],
                        "env": {"MODE": "test"},
                    },
                ),
                (
                    "stdio client",
                    {
                        "command": "mcp-console-dev",
                        "args": ["serve"],
                        "env": {"MODE": "test"},
                    },
                ),
                "stdio enter",
                ("session", "read", "write"),
                "session enter",
                "initialize",
                "list tools",
                ("convert", "send", unittest.mock.ANY, {"strict": True}),
                "body",
                "session exit",
                "stdio exit",
            ],
        )


if __name__ == "__main__":
    unittest.main()
