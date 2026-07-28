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
The command can read the host filesystem, can write regular files only in a dedicated temporary directory, and cannot access the network.
The policy also permits the device and IPC operations needed for supported R and Python workflows, including sandbox-created PTYs and Python multiprocessing semaphores.
Before returning, the launcher terminates descendants observed by the macOS process tracker, including `processx` children that create another session.
A descendant that orphans itself before macOS exposes it to the tracker is outside this initial supervision boundary.
Linux and Windows are not supported yet.

The proposed product and architecture remain under [`design-sketches/`](design-sketches/README.md).

## Development

Run the local checks with:

```bash
scripts/check
```

## License

MCP Console is licensed under the [MIT license](LICENSE).
