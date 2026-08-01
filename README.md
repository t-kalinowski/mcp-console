# `mcp-console`

# 🚧 UNDER CONSTRUCTION 🚧

**This project is not ready for use.**

`mcp-console` is a ground-up rewrite of [`mcp-repl`](https://github.com/posit-dev/mcp-repl).
It applies the lessons learned from `mcp-repl` to a substantially different product---different enough that a new name makes sense.

The repository currently contains the initial Rust binary package.
The following commands are implemented:

```bash
mcp-console
mcp-console serve
mcp-console --version
mcp-console sandbox -- COMMAND [ARG]...
```

`mcp-console` and `mcp-console serve` run a minimal MCP server over stdio.
The server registers one `console` tool that accepts a JSON object and returns that object as JSON text.
It does not execute code or retain state yet.

On macOS, `sandbox` launches the command under `/usr/bin/sandbox-exec`.
The command can open the host filesystem for reading and can open regular files for writing only in a dedicated temporary directory.
Opening and connecting network sockets is denied.
The launcher preserves stdin, stdout, and stderr as explicit capabilities, and closes every other inherited file descriptor when it executes the sandbox.
The policy also permits the device and IPC operations needed for supported R and Python workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
When the launcher owns a terminal, it gives the sandbox command a dedicated foreground process group so terminal-generated signals are delivered once.
`SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` sent directly to the launcher are relayed to that group unless the signal was already blocked or ignored when the launcher started.
The launcher imposes no signal timeout, so a command that handles or ignores a signal may continue running.
Before returning, the launcher terminates descendants observed by the macOS process tracker, including `processx` children that create another session, and waits up to five seconds for them to be reaped.
A process-observation error first attempts to terminate and reap the root process group, then tears down observed descendants.
If root termination cannot be confirmed, the launcher reports both failures and preserves its temporary directory instead of running teardown that assumes the root exited.
Detached descendants may remain when supervision itself fails because their identities can no longer be verified safely.
A descendant that orphans itself before macOS exposes it to the tracker is outside this initial supervision boundary.
Stopped and continued job-control states are not proxied: `Ctrl-Z` is unsupported, and the launcher is intended to supervise a single foreground command rather than act as one stage of an interactive terminal pipeline.
Linux and Windows are not supported yet.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Run the local checks with:

```bash
scripts/check
```

## License

MCP Console is licensed under the [MIT license](LICENSE).
