# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

The repository currently contains the initial Rust binary package.
The following commands are implemented:

```bash
mcp-console serve
mcp-console --help
mcp-console help [COMMAND]
mcp-console --version
mcp-console sandbox -- COMMAND [ARG]...
```

`mcp-console` requires a subcommand.
`mcp-console serve` runs a minimal MCP server over stdio.
Run `mcp-console --help` or `mcp-console COMMAND --help` for command-line help.
The server registers one `send` tool that accepts one complete `r` code cell.
On macOS, the first call lazily starts a sandboxed embedded R worker.
Later calls reuse the same global R state.
The worker runs each cell through R's native top-level loop, captures R console output, prints each visible value, and maintains `.Last.value`.
If a cell ends while an expression is incomplete, earlier complete expressions from that cell remain applied.
Its MCP initialization identity remains `mcp-console`.
The intended default client registration name is `console`:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the implemented tool is `mcp__console.send`; the planned environment and lifecycle tool will be `mcp__console.session`.

On macOS, `sandbox` launches the command under `/usr/bin/sandbox-exec`.
The command can open the host filesystem for reading and can open regular files for writing only in a dedicated temporary directory.
Opening and connecting network sockets is denied.
The launcher preserves stdin, stdout, and stderr for the public `sandbox` command and closes every other inherited file descriptor when it executes that command.
The workers instead connect their standard streams to `/dev/null` and explicitly inherit their two sideband pipes.
The policy also permits the device and IPC operations needed for supported R and Python workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
When the launcher owns a terminal, it gives the sandbox command a dedicated foreground process group so terminal-generated signals are delivered once.
`SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` sent directly to the launcher are relayed to that group unless the signal was already blocked or ignored when the launcher started.
The launcher imposes no signal timeout, so a command that handles or ignores a signal may continue running.
For the public `sandbox` command, before returning, the launcher terminates descendants observed by the macOS process tracker, including `processx` children that create another session, and waits up to five seconds for them to be reaped.
A process-observation error attempts to terminate the root process group and reap the direct sandbox process.
If root termination cannot be confirmed, the launcher reports both failures and preserves its temporary directory instead of running teardown that assumes the root exited.
Detached descendants may remain when supervision itself fails because their identities can no longer be verified safely.
A descendant that orphans itself before macOS exposes it to the tracker is outside this initial supervision boundary.
Stopped and continued job-control states are not proxied: `Ctrl-Z` is unsupported, and the launcher is intended to supervise a single foreground command rather than act as one stage of an interactive terminal pipeline.
Workers do not run this process tracker.
The `sandbox` command and workers are not supported on Linux or Windows.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Run the local checks with:

```bash
scripts/check
```

Format the repository with each installed formatter:

```bash
scripts/format
```

Formatter errors remain visible but do not stop the remaining formatters or make the script fail.

See [`tests/transcripts/README.md`](tests/transcripts/README.md) for running and authoring external server transcript tests.
The `r` suite exercises the built-in worker.
The `zod` suite uses the hidden `serve --worker PATH` development option to exercise the same protocol with an executable Python fixture.
Both suites run on macOS, where the sandbox policy is implemented.
See [`docs/WORKER_PROTOCOL.md`](docs/WORKER_PROTOCOL.md) for the exact implemented launch and message contract.

## License

MCP Console is licensed under the [MIT license](LICENSE).
