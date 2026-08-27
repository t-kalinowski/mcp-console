# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

MCP Console is being built as a persistent, sandboxed R, Python, and DuckDB SQL console for MCP agents.
It gives an MCP client one live computational workspace instead of a sequence of disposable shell commands.
An agent can submit complete R, Python, or SQL cells, keep state across calls, answer interactive prompts, inspect partial output, and switch languages as a task evolves.

The built-in worker embeds R.
Python runs through reticulate, and SQL runs through a persistent DuckDB connection.
R and Python can access one another's globals through reticulate, while DuckDB can query data frames in the R workspace directly.
R plots made with the default device and open Matplotlib figures are returned as images, SQL results are returned as bounded previews, and long-running work can be polled or interrupted.

## Install

MCP Console is currently distributed as native wheels for Apple Silicon and Intel macOS.
Linux and Windows are not supported yet.

A working R installation is required.
Set `R_HOME` or make `R` discoverable on `PATH`.
The Python package installs `r-lib-ir` into the same uv tool environment; it supplies the `ir` command used to prepare R libraries.
The first server start may download and install the default R and Python requirements.

Run the published command without installing it persistently:

```sh
uvx mcp-console --help
uvx mcp-console serve
```

Or install it as a persistent uv tool:

```sh
uv tool install mcp-console

mcp-console --help
mcp-console serve
```

`mcp-console serve` communicates with its MCP client over standard input and output.
It waits for MCP protocol input rather than presenting an interactive terminal prompt.

## Python integrations

The Python package exposes `MCPConsole`, a stateful async connection to `mcp-console serve`.
It owns only the local MCP subprocess and MCP client session.
The application continues to create and own its chat client, agent, Codex client, threads, and tool loop.

Install the extra for the interface you use:

```sh
pip install "mcp-console[client]"
pip install "mcp-console[chatlas]"
pip install "mcp-console[openai]"
pip install "mcp-console[openai-agents]"
pip install "mcp-console[anthropic]"
pip install "mcp-console[codex]"
pip install "mcp-console[codex-sdk]"  # legacy package linked below
```

All integrations default to the `mcp-console` executable installed in the current Python environment and the `serve` argument.
Pass `command=` and `args=` to `MCPConsole` or an individual helper to override that command.

### Callable `send`

`MCPConsole.send` mirrors the server's sole MCP tool and preserves one console session across calls:

```python
from mcp_console import MCPConsole

async with MCPConsole() as console:
    print(await console.send(r="x <- 6 * 7; x"))
    print(await console.send(python="r.x + 1"))
```

The object is also callable, so `await console(python="...")` is equivalent.
This function-style surface is useful with frameworks that register ordinary Python callables.
It returns text and compact placeholders for non-text MCP content.
Use the native MCP integrations below when complete multimodal MCP results are important.

### chatlas

Register the native stdio MCP server on a chat object that the application already owns:

```python
from chatlas import ChatOpenAI
from mcp_console import MCPConsole

chat = ChatOpenAI()
console = MCPConsole()
await console.register_chatlas(chat)

try:
    await chat.chat_async("Use the console to calculate the first 20 Fibonacci numbers.")
finally:
    await chat.cleanup_mcp_tools()
```

For function-style registration, keep the console connection alive and register its bound method:

```python
async with MCPConsole() as console:
    chat.register_tool(console.send)
    await chat.chat_async("Use the console to calculate the first 20 Fibonacci numbers.")
```

### OpenAI Responses API

`openai_responses_tool()` returns a small dispatcher object.
Its `definition` slots into the standard `tools=` argument, and calling the object with a response function call produces the corresponding `function_call_output` item:

```python
from openai import AsyncOpenAI
from mcp_console import MCPConsole

client = AsyncOpenAI()

async with MCPConsole() as console:
    tool = console.openai_responses_tool()
    response = await client.responses.create(
        model="your-model",
        input="Use the console to calculate the first 20 Fibonacci numbers.",
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

The application owns the OpenAI client and response loop.
The dispatcher reads the live MCP tool schema and preserves text and image output, but owns no model state.

### OpenAI Agents SDK

The most direct Agents integration returns the SDK's native `MCPServerStdio`, which is supplied through the normal `Agent.mcp_servers` field:

```python
from agents import Agent, Runner
from mcp_console import MCPConsole

console = MCPConsole()

async with console.openai_agents_server() as server:
    agent = Agent(
        name="Data analyst",
        instructions="Use MCP Console for calculations and data analysis.",
        mcp_servers=[server],
    )
    result = await Runner.run(agent, "Fit a line through (1, 2), (2, 4), and (3, 5).")
    print(result.final_output)
```

A function-tool form is also available:

```python
async with MCPConsole() as console:
    agent = Agent(
        name="Data analyst",
        tools=[console.openai_agents_tool()],
    )
    result = await Runner.run(agent, "Use the console to calculate 20!.")
```

In both cases, MCP Console creates neither the `Agent` nor the `Runner`.

### Anthropic Python SDK

The callable form returns a native Anthropic async function tool that can be supplied directly to the SDK's tool runner:

```python
from anthropic import AsyncAnthropic
from mcp_console import MCPConsole

client = AsyncAnthropic()

async with MCPConsole() as console:
    runner = client.beta.messages.tool_runner(
        model="your-model",
        max_tokens=4096,
        tools=[console.anthropic_tool()],
        messages=[
            {
                "role": "user",
                "content": "Use the console to calculate the first 20 Fibonacci numbers.",
            }
        ],
    )
    async for message in runner:
        print(message)
```

For native MCP conversion, including richer MCP content handling, use `console.anthropic_tools()` as the context manager and pass the yielded list to `tools=`.

### Codex Python SDK

The current official `openai-codex` package exposes a per-thread `config=` argument on `thread_start()`.
`codex_config()` returns an ordinary mapping that slots into that argument:

```python
from openai_codex import Codex
from mcp_console import MCPConsole

console = MCPConsole()

with Codex() as codex:
    thread = codex.thread_start(config=console.codex_config())
    result = thread.run("Use MCP Console to inspect and summarize measurements.csv.")
    print(result.final_response)
```

The older `openai-codex-sdk` 0.1.11 package linked in the original request exposes neither thread-level tools nor a generic Codex config argument.
There is therefore no native tool object to supply to a thread.
A compatibility context manager is available, but it yields only constructor options; the application still creates and owns the Codex client and threads:

```python
from openai_codex_sdk import Codex
from mcp_console import MCPConsole

console = MCPConsole()

with console.openai_codex_sdk_options() as options:
    codex = Codex(options)
    thread = codex.start_thread()
    result = await thread.run("Use MCP Console to inspect measurements.csv.")
```

The compatibility helper uses a temporary Codex executable shim to supply MCP configuration because that older SDK has no native insertion point.

## What it is useful for

MCP Console is intended for iterative computational work: inspecting and transforming data, fitting models, running simulations, making plots, debugging code, and checking exact results.

An agent can load data once, build useful objects, inspect an intermediate result in another language, and continue without reconstructing its environment or passing every intermediate value through files and model context.

## Working with the console

The MCP interface exposes one tool: `send`.
It runs one complete R, Python, or SQL cell, supplies interactive input, prepares additive requirements, applies an optional interrupt or restart, or collects pending output.

Code-bearing calls to `send` are sequential.
A control-only interrupt may overlap a pending `send` while that call resolves or prepares requirements, including for restart.
Requirements alone perform standalone preparation without starting an initial worker.
With a cell, requirements are its preconditions; without control, preparation precedes nonempty standard input and evaluation.
`control = "interrupt"` preserves in-memory state and orders signal delivery, same-call input, and a 100-millisecond grace period; when a cell follows, its requirements are then prepared before evaluation, and the cell is not run if the interrupted evaluation remains active.
`control = "restart"` resolves requirements before replacing the worker, then queues same-call input and runs an optional cell only in the replacement.
Polling and stdin remain code-free `send` calls.
Control, interrupt grace, and explicit requirement preparation do not consume the wait timeout, which starts after cell dispatch or attachment to an active evaluation.
R and Python global state and the in-memory DuckDB catalog remain available until the worker is restarted, replaced after failure, or the server exits.
Prepared requirements remain available across worker restarts, but in-memory language, database, debugger, and unread-input state does not.
Requirements declared on `send` are prepared before its cell runs and remain available to later cells.
Preparation makes packages and extensions available; it does not import, attach, or load them.
The built-in worker resolves missing plain R packages and managed Python imports on demand.
Use packages directly; declare an explicit Python requirement when exact distribution metadata is needed or automatic inference asks for it.
Successful package additions survive restart, while attached packages, imported modules, and other in-memory state do not.

## Example workflow

An agent investigating `measurements.csv` could load the data and fit a model in one R cell submitted through `send`:

```r
measurements <- readr::read_csv(
  "measurements.csv",
  show_col_types = FALSE
)

fit <- lm(response ~ temperature + group, data = measurements)
measurements$.residual <- residuals(fit)
```

It could then query the live R data frame with DuckDB SQL:

```sql
SELECT
  "group",
  count(*) AS n,
  avg(abs(".residual")) AS mean_abs_residual
FROM measurements
GROUP BY "group"
ORDER BY mean_abs_residual DESC
```

And inspect or plot the same data from Python:

```python
frame = r.measurements

import matplotlib.pyplot as plt

plt.scatter(frame["temperature"], frame[".residual"])
plt.axhline(0)
```

The data, model, Python imports, and DuckDB catalog remain available for later calls until the runtime is restarted or replaced.

## Current status

The repository contains a working Rust MCP server, sandboxed worker relay, built-in mixed-language worker, host dependency resolvers, session recording, and public process-boundary transcript tests.
The registered MCP surface contains only `send`.

The core console and its initial PyPI distribution currently support only macOS.
Linux and Windows support is not implemented.
The project remains under active construction.

The server records a JSONL journal of tool calls and results together with image artifacts.
It projects each journal event into a Yamark-formatted, append-only `transcript.md` with syntax-highlighted R, Python, and SQL source, text results, and relative artifact links.
Alongside it, the server regenerates a Yamark-formatted `transcript.qmd` from incremental source and requirement state when submitted code or declared R or Python requirements change.
The QMD contains only submitted executable code cells and IR front matter with the built-in requirements and cumulative declarations.
With the PyPI package, run `uvx --from r-lib-ir ir render transcript.qmd` to execute those client-authored cells in order and export a fresh report using reticulate's default managed Python selection.
When `ir` is installed separately, `ir render transcript.qmd` is equivalent.
The projection is intended to reproduce the analysis represented by `transcript.md`, but it does not include recorded output or artifacts and does not yet reconstruct every runtime detail.
Human-facing tools for following and inspecting an agent's work remain future design.

Other current limitations include:

- there is one implicit session and no named-session management;
- cells run sequentially, while lifecycle control may overlap the operation it interrupts or replaces; and
- restart and worker replacement discard R, Python, DuckDB, debugger, and unread-input state.

## Security boundary

Submitted R, Python, and SQL have shell-class capability inside the worker sandbox.
The worker can read host files, but direct network access and regular-file writes outside its private temporary directory are denied.
This is a process boundary, not a safe evaluator for untrusted code with access to sensitive readable files.

The server installs automatically inferred or explicitly declared R and Python packages and DuckDB extensions outside the worker sandbox with server permissions.
Those operations may access the network and execute installation or build code, so only trusted requirements should be supplied.
See [Requirements and environments](https://github.com/t-kalinowski/mcp-console/blob/main/docs/REQUIREMENTS.md) for the accepted inputs and trust model.

Session journals and the Markdown projection record submitted source, standard input, declared requirements, result text, and artifact paths without redaction.
Image bytes are stored in separate artifact files linked from those records.
The source-only Quarto document contains submitted code and declared R and Python requirements without redaction.
Rendering it executes that source outside the MCP Console worker sandbox with the permissions of the `ir` and Quarto processes.
Render only code you trust.
See [Implemented architecture](https://github.com/t-kalinowski/mcp-console/blob/main/docs/ARCHITECTURE.md) for recording and process placement.

## Development

The implemented commands are:

```text
mcp-console serve
mcp-console sandbox -- COMMAND [ARG]...
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
```

`mcp-console serve` communicates with its MCP client over standard input and output.
The standalone `sandbox` command is available for development, but it supervises only its direct child.
Use the MCP server for the supported worker-generation lifecycle.

Run development commands from the repository root:

```text
scripts/format
scripts/check
scripts/test [BOUNDARY/SUITE[::CASE]]
scripts/test --list
scripts/test --update BOUNDARY/SUITE[::CASE]
```

`scripts/format` attempts each installed formatter and leaves failures visible while continuing with the remaining formatters.
`scripts/check` validates extracted runtime sources, checks the Python integrations, checks Rust formatting and Clippy, runs Rust tests, and runs the complete transcript suite.

## Documentation

The [documentation index](https://github.com/t-kalinowski/mcp-console/blob/main/docs/README.md) maps current documents by audience.

- [Implemented architecture](https://github.com/t-kalinowski/mcp-console/blob/main/docs/ARCHITECTURE.md) explains current process boundaries, ownership, lifecycle, recording, and artifacts.
- [Built-in runtime](https://github.com/t-kalinowski/mcp-console/blob/main/docs/BUILTIN_RUNTIME.md) describes user-visible R, Python, SQL, input, output, and graphics behavior.
- [Requirements and environments](https://github.com/t-kalinowski/mcp-console/blob/main/docs/REQUIREMENTS.md) describes dependency preparation and its trust boundary.
- [Worker protocol](https://github.com/t-kalinowski/mcp-console/blob/main/docs/WORKER_PROTOCOL.md) and [relay protocol](https://github.com/t-kalinowski/mcp-console/blob/main/docs/RELAY_PROTOCOL.md) define the exact transport contracts.
  [Registered tool descriptions](https://github.com/t-kalinowski/mcp-console/blob/main/docs/TOOL_DESCRIPTIONS.md) is a human-readable mirror of the current agent-facing wording.
- [Transcript test guide](https://github.com/t-kalinowski/mcp-console/blob/main/tests/transcripts/README.md) explains selectors, normalization, and golden updates.
- The [release guide](https://github.com/t-kalinowski/mcp-console/blob/main/RELEASE.md) describes PyPI setup, publication, verification, and recovery.

The [project vision](https://github.com/t-kalinowski/mcp-console/blob/main/design-sketches/VISION.md) and other documents under [`design-sketches/`](https://github.com/t-kalinowski/mcp-console/blob/main/design-sketches/README.md) describe intended or exploratory future design, not the implemented system.
When current prose and implementation disagree, source and public acceptance tests are authoritative.

## License

MCP Console is licensed under the [MIT license](https://github.com/t-kalinowski/mcp-console/blob/main/LICENSE).
