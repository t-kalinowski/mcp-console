# MCP Console documentation

The documents under `docs/` describe the system implemented in this repository.
Explanatory documents cover current behavior and ownership, and protocol documents define exact transport interfaces.
The tool description document is a human-readable mirror.
Source and public acceptance tests remain the final authority when prose disagrees with the implementation.

## Project and console users

- The [project README](../README.md) is the short product overview and current project-status page.
- The [ellmer R package](../r/README.md) explains how to install and register MCP Console as an ellmer tool.
- [Built-in runtime](BUILTIN_RUNTIME.md) is the source of truth for user-visible R, Python, DuckDB SQL, input, output, graphics, and interoperability behavior.
- [Requirements and environments](REQUIREMENTS.md) is the source of truth for dependency preparation, retained environments, accepted requirement syntax, and the host-resolution trust boundary.

## Implementers and protocol reviewers

- [Implemented architecture](ARCHITECTURE.md) is the source of truth for the current process structure, responsibility boundaries, worker-generation ownership, and lifecycle at an architectural level.
- [macOS sandbox supervision](SANDBOX_SUPERVISION.md) describes primary host-side lifetime ownership, standalone terminal and signal ownership, manager failure recovery, and the remaining post-spawn boundary.
- [Worker protocol](WORKER_PROTOCOL.md) is the exact relay-worker wire protocol and custom-worker contract.
- [Relay protocol](RELAY_PROTOCOL.md) is the exact private server-relay JSONL protocol.
- [Registered MCP tool descriptions](TOOL_DESCRIPTIONS.md) mirrors the current descriptions for the MCP tools and their properties.

## Test contributors

- The [boundary test guide](../tests/boundaries/README.md) is the source of truth for process boundaries, selectors, normalization, and snapshot updates.
- [`AGENTS.md`](../AGENTS.md) contains repository-wide maintenance rules and the source and test navigation map.

## Maintainers

- The [release guide](../RELEASE.md) describes PyPI setup, publication, verification, and recovery.

## Future design

The documents under [`design-sketches/`](../design-sketches/README.md) describe intended or exploratory future behavior.
They are not documentation of the implemented system and must not be used as evidence of current behavior.
