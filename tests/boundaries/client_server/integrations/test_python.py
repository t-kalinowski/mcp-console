#!/usr/bin/env -S uv run --script

import asyncio
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

import mcp_console
from mcp_console import AsyncMCPConsole, MCPConsole
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import WORKER, requires
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite


def options(binary: Path, execution: Execution) -> dict:
    return {
        "command": binary,
        "args": execution.serve("--worker", str(ROOT / "tests/fixtures/zod")),
        "server_parameters": {
            "env": {**os.environ, "MCP_CONSOLE_TEST_PYTHON": sys.executable}
        },
    }


def assert_callable_schema(schema: dict, description: str) -> None:
    from jsonschema import Draft202012Validator

    assert "Send at most one of r, python, or sql per call." in description, description
    validator = Draft202012Validator(schema)
    # Supply all fields for SDKs that generate strict schemas.
    arguments = dict.fromkeys(schema["properties"])
    arguments.update(
        timeout_ms=60_000,
        requirements={"r": [], "python": ["numpy"], "duckdb": ["spatial"]},
    )
    validator.validate(arguments)
    for timeout in (0, 60_000, 2**64 - 1):
        validator.validate(arguments | {"timeout_ms": timeout})
    for timeout in (-1, 2**64):
        assert not validator.is_valid(arguments | {"timeout_ms": timeout}), schema[
            "properties"
        ]["timeout_ms"]
    requirements = arguments["requirements"]
    empty_requirements = {key: [] for key in requirements}
    validator.validate(arguments | {"requirements": None})
    for empty in ({}, empty_requirements):
        assert not validator.is_valid(arguments | {"requirements": empty}), schema
    for language in ("r", "python", "duckdb"):
        validator.validate(
            arguments | {"requirements": empty_requirements | {language: ["a"]}}
        )
        assert not validator.is_valid(arguments | {"requirements": {language: []}}), (
            schema
        )
        for packages in ([], ["a"] * 64, ["a" * (64 if language == "duckdb" else 65)]):
            validator.validate(
                arguments | {"requirements": requirements | {language: packages}}
            )
        for packages in ([""], ["a"] * 65):
            assert not validator.is_valid(
                arguments | {"requirements": requirements | {language: packages}}
            ), schema
    assert not validator.is_valid(
        arguments | {"requirements": requirements | {"duckdb": ["a" * 65]}}
    ), schema
    arguments["requirements"]["pip"] = ["numpy"]
    assert not validator.is_valid(arguments), schema


@executions(DIRECT, SANDBOXED)
def test_callable_tools_follow_connected_server_fields(
    binary: Path, execution: Execution
) -> Transcript:
    from agents.tool_context import ToolContext
    from chatlas import ChatOpenAI
    from jsonschema import Draft202012Validator

    for console_type in (AsyncMCPConsole, MCPConsole):
        console = console_type()
        for create_tool in (
            console.openai_responses_tool,
            console.openai_agents_tool,
            console.anthropic_tool,
        ):
            try:
                create_tool()
            except RuntimeError as error:
                assert "connect" in str(error).lower(), error
            else:
                raise AssertionError("disconnected console created a tool")

    async def exercise_tools(console, source, expected_fields):
        live_schema = console.openai_responses_tool().definition["parameters"]
        assert set(live_schema["properties"]) == expected_fields, live_schema
        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.register_tool(console.send)
        chat_tool = chat.get_tools()[0]
        agent = console.openai_agents_tool(failure_error_function=None)
        anthropic = console.anthropic_tool()
        arguments = dict.fromkeys(expected_fields) | {"r": source, "timeout_ms": 60_000}
        encoded = json.dumps(arguments)
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=encoded,
        )
        tools = (
            (
                chat_tool.schema["function"]["parameters"],
                lambda: chat_tool.func(**arguments),
            ),
            (agent.params_json_schema, lambda: agent.on_invoke_tool(context, encoded)),
            (anthropic.to_dict()["input_schema"], lambda: anthropic.call(arguments)),
        )
        outputs = []
        for schema, invoke in tools:
            assert set(schema["properties"]) == expected_fields, schema
            validator = Draft202012Validator(schema)
            validator.validate(arguments)
            for omitted in {"r", "python", "sql", "requirements"} - expected_fields:
                value = {"r": ["praise"]} if omitted == "requirements" else "1"
                assert not validator.is_valid(arguments | {omitted: value}), schema
            output = invoke()
            outputs.append(await output if inspect.isawaitable(output) else output)
        assert outputs[0] == outputs[1] == outputs[2], outputs
        return outputs[0]

    async def exercise():
        transcript = []
        original_parameters = set(inspect.signature(AsyncMCPConsole.send).parameters)
        with tempfile.TemporaryDirectory() as directory:
            custom = options(binary, execution)
            custom["server_parameters"]["env"]["MCP_CONSOLE_LANGUAGES"] = "r"
            bare = {
                "command": binary,
                "args": execution.serve(),
                "server_parameters": {
                    "env": bare_runtime_environment(os.environ.copy(), Path(directory))
                },
            }
            for label, settings, source, expected_fields in (
                (
                    "bare",
                    bare,
                    "1 + 1",
                    {"r", "python", "sql", "control", "stdin", "timeout_ms"},
                ),
                (
                    "custom",
                    custom,
                    "echo configured",
                    {"r", "control", "requirements", "stdin", "timeout_ms"},
                ),
            ):
                async with AsyncMCPConsole(**settings) as console:
                    output = await exercise_tools(console, source, expected_fields)
                with MCPConsole(**settings) as console:
                    assert (
                        await exercise_tools(console, source, expected_fields) == output
                    )
                transcript.append({label: output, "fields": sorted(expected_fields)})
        assert (
            set(inspect.signature(AsyncMCPConsole.send).parameters)
            == original_parameters
        )
        assert set(inspect.signature(MCPConsole.send).parameters) == original_parameters
        return transcript

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_callable_preserves_line_breaks_around_images(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise():
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            return await console.send(r="emit image")

    asynchronous = asyncio.run(exercise())
    with MCPConsole(**options(binary, execution)) as console:
        synchronous = console.send(r="emit image")
    assert (
        synchronous == asynchronous == "before image\n[image/png output]\nafter image\n"
    )
    return [{"output": synchronous}]


@requires(WORKER)
@executions(DIRECT, SANDBOXED)
def test_callable_preserves_mixed_language_state(
    binary: Path, execution: Execution
) -> Transcript:
    settings = {
        "command": binary,
        "args": execution.serve(),
        # MCP omits locale variables by default. Exercise Linux's resulting
        # C locale explicitly on every host, including macOS.
        "server_parameters": {"env": {"LC_ALL": "C"}},
    }
    cells = {
        "r": "answer <- 42; answer",
        "python": "r.answer + 1",
        "sql": "SELECT 42 AS answer",
    }
    running = "\n[running; poll with an empty send]"

    def collected(chunks):
        # Completion without new output is represented by the server's done notice.
        return "".join(
            chunk.removesuffix(running) for chunk in chunks if chunk != "[done]"
        )

    async def exercise():
        async with AsyncMCPConsole(**settings) as console:
            assert await console.connect() is console
            transcript = []
            for language, source in cells.items():
                chunks = [await console(**{language: source}, timeout_ms=0)]
                # A returned send can still be running. Collect its complete output
                # before submitting the next cell, including on a cold R startup.
                while chunks[-1].endswith(running):
                    chunks.append(await console.send())
                transcript.append({language: collected(chunks)})
            return transcript

    transcript = asyncio.run(exercise())
    with MCPConsole(**settings) as console:
        synchronous = []
        for language, source in cells.items():
            chunks = [console(**{language: source}, timeout_ms=0)]
            while chunks[-1].endswith(running):
                chunks.append(console.send())
            synchronous.append({language: collected(chunks)})
    assert synchronous == transcript
    return transcript


@executions(DIRECT, SANDBOXED)
def test_errors_close_and_reconnect(binary: Path, execution: Execution) -> Transcript:
    async def exercise():
        console = AsyncMCPConsole(**options(binary, execution))
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
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tool = console.openai_responses_tool()
            assert isinstance(tool, mcp_console.openai.AsyncResponsesTool)
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
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            console.openai_agents_tool()
            tool = console.openai_agents_tool(
                strict_mode=False, failure_error_function=None
            )
            assert isinstance(tool, FunctionTool)
            assert_callable_schema(tool.params_json_schema, tool.description)
            validate({"requirements": {"r": ["praise"]}}, tool.params_json_schema)
            agent = Agent(name="test", tools=[tool])
            assert agent.tools == [tool]
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
        async with mcp_console.openai.agents_server(
            **settings, params=parameters
        ) as server:
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
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            tool = console.anthropic_tool()
            assert tool.to_dict()["name"] == "send"
            assert_callable_schema(
                tool.to_dict()["input_schema"], tool.to_dict()["description"]
            )
            text = await tool.call({"r": "echo anthropic"})
        async with mcp_console.anthropic.tools(
            **options(binary, execution), tool_kwargs={"defer_loading": True}
        ) as tools:
            assert len(tools) == 1
            assert tools[0].to_dict()["defer_loading"] is True
            images = await tools[0].call({"r": "emit image"})
        return [{"callable": text}, {"native": to_jsonable_python(images)}]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_native_openai_agents_waits_for_console_output(
    binary: Path, execution: Execution
) -> Transcript:
    async def exercise():
        settings = options(binary, execution)
        parameters = settings.pop("server_parameters")
        async with mcp_console.openai.agents_server(
            **settings, params=parameters
        ) as server:
            # The fixture stays blocked beyond the SDK's five-second default.
            result = await server.call_tool("send", {"r": "stall", "timeout_ms": 6_000})
            return [result.model_dump(mode="json", by_alias=True, exclude_none=True)]

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
            await mcp_console.chatlas.register(
                chat, **settings, transport_kwargs=parameters
            )
            tools = chat.get_tools()
            assert len(tools) == 1
            result = await tools[0].func(r="echo chatlas")
            return [{"name": tools[0].name, "value": result.value}]
        finally:
            await chat.cleanup_mcp_tools()

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_chatlas_callable_registers_concrete_schema(
    binary: Path, execution: Execution
) -> Transcript:
    from chatlas import ChatOpenAI

    async def exercise():
        chat = ChatOpenAI(model="unused", api_key="unused")
        async with AsyncMCPConsole(**options(binary, execution)) as console:
            chat.register_tool(console.send)
            tools = chat.get_tools()
            assert [tool.name for tool in tools] == ["send"]
            assert_callable_schema(
                tools[0].schema["function"]["parameters"],
                tools[0].schema["function"]["description"],
            )
            return [{"output": await tools[0].func(r="echo callable chatlas")}]

    return asyncio.run(exercise())


@executions(DIRECT, SANDBOXED)
def test_sync_callable_and_framework_tools(
    binary: Path, execution: Execution
) -> Transcript:
    from agents.tool_context import ToolContext
    from chatlas import ChatOpenAI
    from jsonschema import validate

    console = MCPConsole(**options(binary, execution))
    transcript = []
    with console:
        assert console.connect() is console
        assert not inspect.iscoroutinefunction(console.send)
        transcript.append({"callable": console(r="echo sync")})
        try:
            console.send(r="echo r", python="echo python")
        except RuntimeError as error:
            transcript.append({"error": str(error)})
        else:
            raise AssertionError("invalid send succeeded")

        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.register_tool(console.send)
        chat_tool = chat.get_tools()[0]
        assert not inspect.iscoroutinefunction(chat_tool.func)
        assert_callable_schema(
            chat_tool.schema["function"]["parameters"],
            chat_tool.schema["function"]["description"],
        )
        transcript.append({"chatlas": chat_tool.func(r="echo chatlas")})

        anthropic_tool = console.anthropic_tool()
        assert_callable_schema(
            anthropic_tool.to_dict()["input_schema"],
            anthropic_tool.to_dict()["description"],
        )
        transcript.append({"anthropic": anthropic_tool.call({"r": "echo anthropic"})})

        agent_tool = console.openai_agents_tool(failure_error_function=None)
        assert_callable_schema(agent_tool.params_json_schema, agent_tool.description)
        validate({"requirements": {"r": ["praise"]}}, agent_tool.params_json_schema)
        arguments = json.dumps({"r": "echo agent"})
        context = ToolContext(
            context=None,
            tool_name="send",
            tool_call_id="call_1",
            tool_arguments=arguments,
        )
        transcript.append(
            {"agent": asyncio.run(agent_tool.on_invoke_tool(context, arguments))}
        )
    console.close()
    try:
        console.send()
    except RuntimeError as error:
        transcript.append({"closed": str(error)})
    else:
        raise AssertionError("closed console accepted a call")
    with console:
        transcript.append({"reconnected": console.send(r="echo reconnected")})
        # Closing must also clean up an active worker and the MCP transport.
        assert "running" in console.send(r="stall", timeout_ms=0)
    return transcript


@executions(DIRECT, SANDBOXED)
def test_sync_responses_preserves_text_and_images(
    binary: Path, execution: Execution
) -> Transcript:
    from openai.types.responses import ResponseFunctionToolCall
    from openai.types.responses.response_input_item_param import FunctionCallOutput
    from pydantic import TypeAdapter

    with MCPConsole(**options(binary, execution)) as console:
        tool = console.openai_responses_tool()
        assert isinstance(tool, mcp_console.openai.ResponsesTool)
        assert tool.definition["name"] == "send"
        assert tool.definition["strict"] is False
        outputs = []
        for source in ("echo response", "emit image"):
            result = tool(
                ResponseFunctionToolCall(
                    type="function_call",
                    name="send",
                    call_id="call_1",
                    arguments=json.dumps({"r": source}),
                )
            )
            TypeAdapter(FunctionCallOutput).validate_python(result)
            outputs.append(result)
        assert tool.call({"r": "echo direct"}) == "zod: direct\n"
        return outputs


def test_thread_configuration_preserves_existing_servers(binary: Path) -> Transcript:
    from openai_codex.generated.v2_all import ThreadStartParams

    parameters = {"env": {"MODE": "test"}}
    config = {
        "model": "caller-selected-model",
        "mcp_servers": {
            "existing": {"command": "other"},
            "console": mcp_console.codex.server(
                command="custom-console",
                server_parameters=parameters,
            ),
        },
    }
    assert parameters == {"env": {"MODE": "test"}}
    ThreadStartParams(config=config)
    return [{"config": config}]


if __name__ == "__main__":
    run_this_suite(__file__)
