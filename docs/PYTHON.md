# Python integrations

Install the extra for the interface you use, with Python 3.11 or newer:

| Interface           | Install                                    |
| ------------------- | ------------------------------------------ |
| Python client       | `pip install "mcp-console[client]"`        |
| chatlas             | `pip install "mcp-console[chatlas]"`       |
| OpenAI Responses    | `pip install "mcp-console[openai]"`        |
| OpenAI Agents       | `pip install "mcp-console[openai-agents]"` |
| Anthropic           | `pip install "mcp-console[anthropic]"`     |
| Official thread SDK | `pip install "mcp-console[codex]"`         |

The base package installs the executable without framework dependencies.
The client uses MCP 2.2 or newer within the 2.x series.
Each framework extra declares the minimum SDK release used by the integration tests.

Clients and helpers that open a connection use the executable installed in the current Python environment, then search `PATH`, and launch it with `serve`.
Pass `command=` and `args=` to configure that launch.
The application owns its model clients, agents, chats, and tool loops.
The clients are available at the package root; framework helpers live in the `chatlas`, `openai`, `anthropic`, and `codex` submodules.

## Python clients

Use `MCPConsole` for synchronous code:

```python
from mcp_console import MCPConsole

with MCPConsole() as console:
    print(console.send(r="answer <- 42; answer"))
```

Use `AsyncMCPConsole` for async code:

```python
from mcp_console import AsyncMCPConsole

async with AsyncMCPConsole() as console:
    print(await console.send(python="answer = 42; answer"))
```

Both clients have the same `send()` arguments; `console(...)` is shorthand for a Python call to `console.send(...)`.
These calls return text, represent images with a MIME-type placeholder, and raise `RuntimeError` for MCP tool errors.
The native MCP integrations and Responses adapters preserve image content.

Use the product adapters below for SDK tool registration.
They use the connected server's schema, available as `console.send_tool`, including its configured languages and requirement availability.
Registering the console object or `console.send` through an SDK's function-schema inference is outside this API: the Python annotations describe ordinary calls and do not describe a particular server's capabilities.

Supply at most one of `r`, `python`, or `sql` per call.
The server returns a tool error if multiple code fields are supplied.
The adapters preserve the native MCP schema and leave runtime validation, including this cross-field rule, to the server.

`timeout_ms` limits the wait, not the evaluation.
If the response ends in `[running; poll with an empty send]`, call `send()` again without code or stdin until the evaluation finishes before submitting another cell.
Each poll returns new output.

Use one connection for the lifetime of a conversation.
Python clients use the MCP `initialize` handshake while waiting for local or remote startup to finish.
Explicit `console.connect()` and `console.close()` are also available; await these methods on `AsyncMCPConsole`.
Enter and close an async connection in the same async task.
The synchronous client manages that task on an AnyIO portal thread.
The MCP SDK owns subprocess and transport cleanup.
After closing, reconnecting starts a fresh server and console session.
Create or register tools after connecting, and recreate them after reconnecting.
Pass native stdio settings such as `env` and `cwd` through `server_parameters=`.

## chatlas

Add a tool to an existing chat:

```python
from chatlas import ChatOpenAI
from mcp_console import MCPConsole, chatlas

chat = ChatOpenAI()
with MCPConsole() as console:
    chat.set_tools([*chat.get_tools(), chatlas.tool(console)])
    chat.chat("Use the console to calculate 20!.")
```

`chatlas.tool()` accepts either client and returns a synchronous or asynchronous `chatlas.Tool`.
Use `set_tools()` to preserve its explicit schema; chatlas 0.23.0's `register_tool()` rebuilds schemas from Python annotations.

For native MCP registration, use chatlas's async interface:

```python
import mcp_console
from chatlas import ChatOpenAI

chat = ChatOpenAI()
try:
    await mcp_console.chatlas.register(chat)
    await chat.chat_async("Use the console to calculate 20!.")
finally:
    await chat.cleanup_mcp_tools()
```

chatlas owns the native MCP connection.
Native registration uses chatlas's stdio helper; use its default tool names because chatlas 0.23.0's `namespace=` option also renames the tool sent to the server.

## OpenAI Responses

`mcp_console.openai.responses_tool(console)` accepts either client and reads the live MCP tool schema.
Its `definition` goes in `responses.create(tools=...)`, and calling it with a function call returns a `function_call_output` item containing text and images.

Use the synchronous OpenAI client with `MCPConsole`:

```python
from mcp_console import MCPConsole, openai
from openai import OpenAI

client = OpenAI()
with MCPConsole() as console:
    tool = openai.responses_tool(console)
    response = client.responses.create(
        model="your-model",
        input="Use the console to calculate 20!.",
        tools=[tool.definition],
    )
    while calls := [
        item
        for item in response.output
        if item.type == "function_call" and item.name == "send"
    ]:
        response = client.responses.create(
            model="your-model",
            previous_response_id=response.id,
            input=[tool(call) for call in calls],
            tools=[tool.definition],
        )
    print(response.output_text)
```

For async code, import `AsyncOpenAI` and `AsyncMCPConsole` and use `async with AsyncMCPConsole()`.
Await `client.responses.create(...)` and each `tool(call)`.
The application owns the [Responses function-calling loop](https://developers.openai.com/api/docs/guides/function-calling).
The adapter keeps the optional MCP arguments in a non-strict schema.

## OpenAI Agents

Use a synchronous console with `Runner.run_sync`:

```python
from agents import Agent, Runner
from mcp_console import MCPConsole, openai

with MCPConsole() as console:
    agent = Agent(name="Data analyst", tools=[openai.agents_tool(console)])
    result = Runner.run_sync(agent, "Use the console to calculate 20!.")
    print(result.final_output)
```

`openai.agents_tool()` also accepts `AsyncMCPConsole` for use with `Runner.run`.
It returns the SDK's `FunctionTool` with the live MCP schema and `strict_json_schema=False` to preserve optional arguments.
Tool errors raise `RuntimeError`.

To use the SDK's native MCP support, supply its server to an agent:

```python
import mcp_console
from agents import Agent, Runner

async with mcp_console.openai.agents_server() as server:
    agent = Agent(name="Data analyst", mcp_servers=[server])
    result = await Runner.run(agent, "Use the console to calculate 20!.")
    print(result.final_output)
```

The SDK read deadline is unset by default so the server's `send` timeout governs each call, including long evaluations.
Set `client_session_timeout_seconds=` to supply an SDK deadline.
Pass stdio settings through `params=` and other native SDK options as keyword arguments.

## Anthropic

Use the synchronous client and runner:

```python
from anthropic import Anthropic
from mcp_console import MCPConsole, anthropic

client = Anthropic()
with MCPConsole() as console:
    runner = client.beta.messages.tool_runner(
        model="your-model",
        max_tokens=4096,
        tools=[anthropic.tool(console)],
        messages=[{"role": "user", "content": "Use the console to calculate 20!."}],
    )
    for message in runner:
        print(message)
```

For async code, use `AsyncAnthropic`, `AsyncMCPConsole`, and `async for`.
`anthropic.tool(console)` selects the native sync or async function tool to match the console.

For native MCP tools that preserve images, use `async with mcp_console.anthropic.tools() as tools` and pass `tools` to the async runner.
This context owns its connection.
Pass stdio settings through `server_parameters=` and native conversion options through `tool_kwargs=`.

## Official thread SDK

`mcp_console.codex.server()` returns one stdio server entry for the official `openai-codex` package.
Place it inside your own configuration, alongside other settings and servers:

```python
import mcp_console
from openai_codex import Codex

config = {
    "model": "your-model",
    "mcp_servers": {
        "mcp-console": mcp_console.codex.server(),
    },
}

with Codex() as client:
    thread = client.thread_start(config=config)
    result = thread.run("Use MCP Console to calculate 20!.")
    print(result.final_response)
```

Use `server_parameters=` for the Console entry's stdio configuration.
The helper creates no client, thread, subprocess, or temporary launcher.
