# Python integrations

The Python package supplies sync/async clients and framework adapters around the same persistent MCP session.
It requires Python 3.11 or newer; the executable alone does not require framework dependencies.

From a source checkout, select the appropriate extra:

```sh
uv tool run --python 3.12 --from ".[client]" python your_client.py
```

| Integration | Extra | Adapter |
| --- | --- | --- |
| Python client | `client` | `MCPConsole`, `AsyncMCPConsole` |
| chatlas | `chatlas` | `chatlas.tool`, `chatlas.register` |
| OpenAI Responses | `openai` | `openai.responses_tool` |
| OpenAI Agents | `openai-agents` | `openai.agents_tool`, `openai.agents_server` |
| Anthropic | `anthropic` | `anthropic.tool`, `anthropic.tools` |
| Official thread SDK | `codex` | `codex.server` |

Source installation may build the native bundle; see [installation prerequisites](../RELEASE.md#private-sandbox-executable).
[`pyproject.toml`](../pyproject.toml) owns dependency versions.
Helpers use Console installed in the current Python environment, then `PATH`, and launch `serve`.
Override `command=` and `args=` for another executable; pass stdio `env`/`cwd` through the helper's parameter mapping.

## Python clients

```python
from mcp_console import MCPConsole

with MCPConsole() as console:
    print(console.send(python="answer = 42; answer"))
    print(console.send(sql="SELECT 6 * 7 AS answer"))
```

The async equivalent uses `async with AsyncMCPConsole()` and `await console.send(...)`.
Enter and close an async connection in the same task; the sync wrapper owns an AnyIO portal thread.
Explicit `connect()`/`close()` are also available.
Reconnect starts a new session, so recreate adapters after reconnecting.
The MCP SDK owns subprocess/transport cleanup.

`console(...)` aliases `send(...)` for ordinary Python calls.
It returns text, uses MIME placeholders for images, and raises `RuntimeError` for MCP tool errors.
Native MCP integrations and the Responses adapter preserve images.

Use one connection per conversation, send at most one language cell per call, and collect unfinished work before submitting another cell.
`timeout_ms` limits observation, not execution: after `[running; poll with an empty send]`, call `send()` without code to collect new output.
Never rerun a cell to retrieve its original result.
[Runtime](BUILTIN_RUNTIME.md), [`send` ordering](SEND_OPERATIONS.md), and [recordings](RECORDING.md) define the shared behavior and output limits.

Adapters read the connected server's schema from `console.send_tool`.
Do not register `console.send` through automatic Python annotation inference: its annotations cannot express the particular server's configured capabilities.
The application owns model clients, agents, chats, and tool loops.

## chatlas

```python
from chatlas import ChatOpenAI
from mcp_console import MCPConsole, chatlas

chat = ChatOpenAI()
with MCPConsole() as console:
    chat.set_tools([*chat.get_tools(), chatlas.tool(console)])
    chat.chat("Use the console to calculate 20!.")
```

`tool()` accepts either client and returns the matching sync/async tool.
Use `set_tools()` to retain its explicit schema; `register_tool()` in the tested chatlas 0.23.0 rebuilds it from annotations.
For native async MCP registration, call `await chatlas.register(chat)`, use `chat.chat_async(...)`, and close with `await chat.cleanup_mcp_tools()` in `finally`.
Use default native tool names: the tested `namespace=` option also renames the requested server tool.

## OpenAI Responses

`responses_tool(console)` exposes `.definition` for `responses.create(tools=...)` and returns a `function_call_output` when called with a model function call.
The adapter uses the live non-strict MCP schema and preserves text/images:

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
        item for item in response.output
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

For async code, use `AsyncOpenAI` and `AsyncMCPConsole`, awaiting both API and tool calls.
The application owns the function-calling loop and routing of any other tools.

## OpenAI Agents

```python
from agents import Agent, Runner
from mcp_console import MCPConsole, openai

with MCPConsole() as console:
    agent = Agent(name="Data analyst", tools=[openai.agents_tool(console)])
    result = Runner.run_sync(agent, "Use the console to calculate 20!.")
    print(result.final_output)
```

`agents_tool()` also accepts the async client for `Runner.run`.
It preserves optional fields with `strict_json_schema=False`.
For native MCP, enter `async with openai.agents_server() as server` and pass `[server]` as the agent's `mcp_servers`.
That SDK connection has no read deadline by default; set `client_session_timeout_seconds=` deliberately and supply stdio settings through `params=`.

## Anthropic

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

Use `AsyncAnthropic`, `AsyncMCPConsole`, and `async for` for async execution.
For native image-preserving MCP tools, enter `async with anthropic.tools() as tools` and supply them to the async runner.
That context owns its connection; `server_parameters=` supplies stdio settings and `tool_kwargs=` supplies conversion options.

## Official thread SDK

`codex.server()` returns a stdio configuration entry for `openai-codex`; it creates no client, thread, or process:

```python
from mcp_console import codex
from openai_codex import Codex

config = {"mcp_servers": {"mcp-console": codex.server()}}
with Codex() as client:
    thread = client.thread_start(config=config)
    print(thread.run("Use the console to calculate 20!.").final_response)
```

Add model and other settings in the application configuration; pass Console stdio settings through `server_parameters=`.

## Inspecting and replacing requirements

Both clients accept the shared `get`, `add`, `set`, and `reset` actions.
Once startup has settled, inspection returns complete JSON text:

```python
snapshot = json.loads(console.send(requirements={"action": "get"}))
declaration = snapshot["requirements"]
declaration["python"] = ["requests>=2"]
console.send(control="restart", requirements={**declaration, "action": "set"})
```

This fragment assumes `import json` and an open `console`.
During startup, inspection can instead return `[worker starting]`; wait and retry before parsing JSON.
`set` empties omitted fields; `reset` accepts no payload.
See [requirements](REQUIREMENTS.md#inspecting-and-replacing-requirements) for transaction and target restrictions.
