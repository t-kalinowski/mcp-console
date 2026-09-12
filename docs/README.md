# MCP Console documentation

The documents under `docs/` describe the system implemented in this repository.
Explanatory documents cover current behavior and ownership, and protocol documents define exact transport interfaces.
The tool description guide links to the canonical snapshot of the registered tools and schemas.
Source and public acceptance tests remain the final authority when prose disagrees with the implementation.

## Project and console users

- The [project README](../README.md) is the short product overview and current project-status page.
- The [ellmer R package](../r/README.md) explains how to install and register MCP Console as an ellmer tool.
- [Python integrations](PYTHON.md) covers synchronous and asynchronous clients, framework adapters, optional dependencies, and connection ownership.
- [`send` operation order](SEND_OPERATIONS.md) owns request validation, preparation and control ordering, stdin generation, failure effects, and wait-timeout semantics.
- [Built-in runtime](BUILTIN_RUNTIME.md) is the source of truth for user-visible R, Python, DuckDB SQL, input, output, graphics, and interoperability behavior.
- [Requirements and environments](REQUIREMENTS.md) is the source of truth for dependency preparation, retained environments, accepted requirement syntax, and the host-resolution trust boundary.
- [SSH execution](SSH.md) defines the single remote target, runtime and resolver bootstrap prerequisites, managed preparation and bare-runtime fallback, remote policy paths, transport limits, and local recordings.
- [Docker execution](DOCKER.md) defines image setup, container paths and mounts, preinstalled runtime requirements, sandbox choices, owned container lifetime, and controller recordings.

## Implementers and protocol reviewers

- [Implemented architecture](ARCHITECTURE.md) is the source of truth for the current process structure, responsibility boundaries, worker-generation ownership, and lifecycle at an architectural level.
- [Sandbox integration](SANDBOX.md) describes Console policy defaults, the verified executable handoff, native platform requirements, and lifetime limits.
- [Sandbox configuration](SANDBOX_CONFIGURATION.md) defines explicit JSON environment input, target overrides, lifecycle settings, and shell, Python, and R callers.
- [Worker protocol](WORKER_PROTOCOL.md) is the exact relay-worker wire protocol and custom-worker contract.
- [Relay protocol](RELAY_PROTOCOL.md) is the exact private server-relay JSONL protocol.
- [MCP tool description guidance](TOOL_DESCRIPTIONS.md) covers editorial rules and links to the canonical `tools/list` snapshot.

## Test contributors

- The [boundary test guide](../tests/boundaries/README.md) is the source of truth for process boundaries, selectors, normalization, and snapshot updates.
- [`AGENTS.md`](../AGENTS.md) contains repository-wide maintenance rules and the source and test navigation map.

## Maintainers

- The [release guide](../RELEASE.md) describes PyPI setup, publication, verification, and recovery.
- [Linux compatibility](LINUX_COMPATIBILITY.md) records native capability requirements, procfs security comparisons, backend differences, and helper integrity.
- The [runner integration record](SANDBOX_RUNNER_INTEGRATION.md) records the baseline, validation, changed fixtures and guarantees for the standalone-supervisor migration.

## Future design

The documents under [`design-sketches/`](../design-sketches/README.md) describe intended or exploratory future behavior.
They are not documentation of the implemented system and must not be used as evidence of current behavior.
