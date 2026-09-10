#!/usr/bin/env -S uv run --script

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

from mcp_console import (
    MCPConsole,
    anthropic_tools,
    codex_config,
    openai_agents_server,
    register_chatlas,
)
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import WORKER, requires
from support.suites import run_this_suite


def options(binary: Path, execution: Execution) -> dict:
    return {
        "command": binary,
        "args": execution.serve("--worker", str(ROOT / "tests/fixtures/zod")),
        "server_parameters": {
            "env": {**os.environ, "MCP_CONSOLE_TEST_PYTHON": sys.executable}
        },
    }


@requires(WORKER)
@executions(DIRECT, SANDBOXED)
def test_callable_preserves_mixed_language_state(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise():
        async with MCPConsole(command=binary, args=execution.serve()) as console:
            assert await console.connect() is console
            return [
                {"r": await console(r="answer <- 42; answer")},
                {"python": await console.send(python="r.answer + 1")},
                {"sql": await console.send(sql="SELECT 42 AS answer")},
            ]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_errors_close_and_reconnect(binary: Path, execution: Execution) -> Transcript:
    async def exercise():
        console = MCPConsole(**options(binary, execution))
        transcript = []
        async with console:
            try:
                await console.send(r="echo r", python="echo python")
            except RuntimeError as error:
                transcript.append({"error": str(error)})
            else:
                raise AssertionError("invalid send succeeded")
            transcript.append({"recovery": await console.send(r="echo recovered")})
        await console.close()
        try:
            await console.send()
        except RuntimeError as error:
            transcript.append({"closed": str(error)})
        else:
            raise AssertionError("closed console accepted a call")
        async with console:
            transcript.append({"reconnected": await console.send(r="echo reconnected")})
        return transcript

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_responses_preserves_schema_text_and_images(
    binary: Path, execution: Execution
) -> Transcript:
    from openai.types.responses import ResponseFunctionToolCall
    from openai.types.responses.response_input_item_param import FunctionCallOutput
    from pydantic import TypeAdapter

    async def exercise():
        async with MCPConsole(**options(binary, execution)) as console:
            tool = console.openai_responses_tool()
            schema = tool.definition["parameters"]
            assert set(schema["properties"]) == {
                "r",
                "python",
                "sql",
                "stdin",
                "control",
                "requirements",
                "timeout_ms",
            }
            assert tool.definition["strict"] is False
            outputs = []
            for source in ("echo response", "emit image"):
                result = await tool(
                    ResponseFunctionToolCall(
                        type="function_call",
                        name="send",
                        call_id="call_1",
                        arguments=json.dumps({"r": source}),
                    )
                )
                TypeAdapter(FunctionCallOutput).validate_python(result)
                outputs.append(result)
            return outputs

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_openai_agents_callable_preserves_optional_arguments(
    binary: Path, execution: Execution
) -> Transcript:
    from agents import Agent, FunctionTool
    from agents.tool_context import ToolContext
    from jsonschema import validate

    async def exercise():
        # Construction itself must work with the SDK's default schema handling.
        console = MCPConsole(**options(binary, execution))
        console.openai_agents_tool()
        tool = console.openai_agents_tool(
            strict_mode=False, failure_error_function=None
        )
        assert isinstance(tool, FunctionTool)
        validate(
            {"r": "echo agent", "requirements": {"python": ["numpy"]}},
            tool.params_json_schema,
        )
        agent = Agent(name="test", tools=[tool])
        assert agent.tools == [tool]
        async with console:
            arguments = json.dumps({"r": "echo agent"})
            context = ToolContext(
                context=None,
                tool_name="send",
                tool_call_id="call_1",
                tool_arguments=arguments,
            )
            output = await tool.on_invoke_tool(context, arguments)
            return [{"output": output}]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_native_openai_agents_server(binary: Path, execution: Execution) -> Transcript:
    from agents import Agent
    from agents.mcp import MCPServerStdio

    async def exercise():
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        async with openai_agents_server(**settings, params=parameters) as server:
            assert isinstance(server, MCPServerStdio)
            agent = Agent(name="test", mcp_servers=[server])
            assert agent.mcp_servers == [server]
            tools = await server.list_tools()
            assert [tool.name for tool in tools] == ["send"]
            result = await server.call_tool("send", {"r": "emit image"})
            return [result.model_dump(mode="json", by_alias=True, exclude_none=True)]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_anthropic_callable_and_native_tools(
    binary: Path, execution: Execution
) -> Transcript:
    from pydantic_core import to_jsonable_python

    async def exercise():
        async with MCPConsole(**options(binary, execution)) as console:
            tool = console.anthropic_tool()
            assert tool.to_dict()["name"] == "send"
            text = await tool.call({"r": "echo anthropic"})
        async with anthropic_tools(
            **options(binary, execution), tool_kwargs={"defer_loading": True}
        ) as tools:
            assert len(tools) == 1
            assert tools[0].to_dict()["defer_loading"] is True
            images = await tools[0].call({"r": "emit image"})
        return [{"callable": text}, {"native": to_jsonable_python(images)}]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_chatlas_registers_on_existing_chat(
    binary: Path, execution: Execution
) -> Transcript:
    from chatlas import ChatOpenAI

    async def exercise():
        chat = ChatOpenAI(model="unused", api_key="unused")
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        try:
            await register_chatlas(chat, **settings, transport_kwargs=parameters)
            tools = chat.get_tools()
            assert len(tools) == 1
            result = await tools[0].func(r="echo chatlas")
            return [{"name": tools[0].name, "value": result.value}]
        finally:
            await chat.cleanup_mcp_tools()

    return asyncio.run(exercise())


def test_thread_configuration_preserves_existing_servers(binary: Path) -> Transcript:
    from openai_codex.generated.v2_all import ThreadStartParams

    existing = {"mcp_servers": {"existing": {"command": "other"}}}
    config = codex_config(
        command="custom-console",
        config=existing,
        server_parameters={"env": {"MODE": "test"}},
    )
    assert existing == {"mcp_servers": {"existing": {"command": "other"}}}
    ThreadStartParams(config=config)
    return [{"config": config}]


if __name__ == "__main__":
    run_this_suite(__file__)
