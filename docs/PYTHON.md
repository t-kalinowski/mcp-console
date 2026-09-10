# Python integrations

Install the extra for the interface you use, with Python 3.10 or newer:

| Interface           | Install                                    |
| ------------------- | ------------------------------------------ |
| Callable client     | `pip install "mcp-console[client]"`        |
| chatlas             | `pip install "mcp-console[chatlas]"`       |
| OpenAI Responses    | `pip install "mcp-console[openai]"`        |
| OpenAI Agents       | `pip install "mcp-console[openai-agents]"` |
| Anthropic           | `pip install "mcp-console[anthropic]"`     |
| Official thread SDK | `pip install "mcp-console[codex]"`         |

The base package installs the executable without framework dependencies.
The client uses MCP 2.2 or newer within the 2.x series.
Each framework extra declares the minimum SDK release used by the integration tests.

All helpers default to the executable installed in the current Python environment, then search `PATH`, and launch it with `serve`.
Pass `command=` and `args=` to override the command.
The application owns its model clients, agents, chats, and tool loops.

## Callable client

```python
from mcp_console import MCPConsole

async with MCPConsole() as console:
    print(await console.send(r="answer <- 42; answer"))
    print(await console(python="r.answer + 1"))
```

`send()` mirrors the server tool, and calling the object is equivalent.
It returns text, represents images with a MIME-type placeholder, and raises `RuntimeError` for MCP tool errors.
The native MCP integrations and Responses adapter preserve image content.

Use one connection for the lifetime of a conversation.
Enter and close it in the same async task; the MCP SDK owns its subprocess and transport cleanup.
Explicit `await console.connect()` and `await console.close()` are also available.
After closing, reconnecting starts a fresh server and console session.
Pass native stdio settings such as `env` and `cwd` through `server_parameters=`.

## chatlas

Register the server on an existing chat:

```python
from chatlas import ChatOpenAI
from mcp_console import register_chatlas

chat = ChatOpenAI()
try:
    await register_chatlas(chat)
    await chat.chat_async("Use the console to calculate 20!.")
finally:
    await chat.cleanup_mcp_tools()
```

chatlas owns this connection.
With an existing `MCPConsole` connection, `chat.register_tool(console.send)` registers the callable instead.
Native registration uses chatlas's stdio helper; use its default tool names because chatlas 0.23.0's `namespace=` option also renames the tool sent to the server.

## OpenAI Responses

The adapter reads the live MCP tool schema.
Its `definition` goes in `responses.create(tools=...)`, and calling it with a function call returns a `function_call_output` item containing text and images.

```python
from openai import AsyncOpenAI
from mcp_console import MCPConsole

client = AsyncOpenAI()
async with MCPConsole() as console:
    tool = console.openai_responses_tool()
    response = await client.responses.create(
        model="your-model",
        input="Use the console to calculate 20!.",
        tools=[tool.definition],
    )
    while calls := [
        item
        for item in response.output
        if item.type == "function_call" and item.name == "send"
    ]:
        response = await client.responses.create(
            model="your-model",
            previous_response_id=response.id,
            input=[await tool(call) for call in calls],
            tools=[tool.definition],
        )
    print(response.output_text)
```

The application owns the [Responses function-calling loop](https://developers.openai.com/api/docs/guides/function-calling).
The adapter keeps the optional MCP arguments in a non-strict schema.

## OpenAI Agents

Supply the native server to an agent:

```python
from agents import Agent, Runner
from mcp_console import openai_agents_server

async with openai_agents_server(client_session_timeout_seconds=None) as server:
    agent = Agent(name="Data analyst", mcp_servers=[server])
    result = await Runner.run(agent, "Use the console to calculate 20!.")
    print(result.final_output)
```

`client_session_timeout_seconds=None` lets the server's `send` timeout govern each call, including long evaluations.
Pass stdio settings through `params=` and other native SDK options as keyword arguments.
For a connected `MCPConsole`, `console.openai_agents_tool()` returns a native function tool for `Agent(tools=[...])`.
It uses a non-strict schema to preserve optional arguments and sparse requirement dictionaries.

## Anthropic

Supply native MCP-converted tools to the SDK's runner:

```python
from anthropic import AsyncAnthropic
from mcp_console import anthropic_tools

client = AsyncAnthropic()
async with anthropic_tools() as tools:
    runner = client.beta.messages.tool_runner(
        model="your-model",
        max_tokens=4096,
        tools=tools,
        messages=[{"role": "user", "content": "Use the console to calculate 20!."}],
    )
    async for message in runner:
        print(message)
```

The context owns the MCP connection; pass stdio settings through `server_parameters=` and native conversion options through `tool_kwargs=`.
For a connected `MCPConsole`, `console.anthropic_tool()` returns a native async function tool wrapping `send()`.

## Official thread SDK

`codex_config()` returns a mapping for the official `openai-codex` package's `thread_start(config=...)` argument:

```python
from openai_codex import Codex
from mcp_console import codex_config

with Codex() as client:
    thread = client.thread_start(config=codex_config())
    result = thread.run("Use MCP Console to inspect measurements.csv.")
    print(result.final_response)
```

Pass an existing mapping through `config=` to retain other settings and MCP servers.
Use `server_name=` to select the Console entry and `server_parameters=` for its stdio configuration.
The helper creates no client, thread, subprocess, or temporary launcher.
