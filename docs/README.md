# Documentation

These pages describe the implemented system.
Start with the [user quickstart](../README.md), the [development workflow](DEVELOPMENT.md), or the [architecture](ARCHITECTURE.md).
The [glossary](GLOSSARY.md) defines shared terms.
Source and public acceptance tests take precedence over prose; [`design-sketches/`](../design-sketches/) describes possible future work.

## Using Console

| Question                                             | Guide                                                                   |
| ---------------------------------------------------- | ----------------------------------------------------------------------- |
| How do I configure a session?                        | [Configuration](CONFIGURATION.md)                                       |
| How do cells, input, plots, and languages work?      | [Built-in runtime](BUILTIN_RUNTIME.md)                                  |
| What happens when a call combines actions?           | [`send` operations](SEND_OPERATIONS.md)                                 |
| How are dependencies selected and changed?           | [Requirements](REQUIREMENTS.md)                                         |
| Where are transcripts, retained output, and exports? | [Recordings](RECORDING.md)                                              |
| How do I connect from Python or R?                   | [Python](PYTHON.md), [ellmer](../r/README.md)                           |
| How do I run elsewhere?                              | [SSH](SSH.md), [Docker](DOCKER.md), [Docker Sandbox](DOCKER_SANDBOX.md) |
| What works on Windows?                               | [Windows local execution](WINDOWS.md)                                   |
| What can evaluated code access?                      | [Sandbox](SANDBOX.md), [policy configuration](SANDBOX_CONFIGURATION.md) |

## Changing Console

| Question                                | Guide                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------ |
| Who owns state, processes, and cleanup? | [Architecture](ARCHITECTURE.md)                                                |
| What crosses the internal transports?   | [Relay protocol](RELAY_PROTOCOL.md), [worker protocol](WORKER_PROTOCOL.md)     |
| How should MCP tool prose be written?   | [Tool descriptions](TOOL_DESCRIPTIONS.md)                                      |
| How do I develop and test a change?     | [Development](DEVELOPMENT.md), [boundary tests](../tests/boundaries/README.md) |
| What does Linux enforcement require?    | [Linux compatibility](LINUX_COMPATIBILITY.md)                                  |
| How do I build and publish a release?   | [Release](../RELEASE.md)                                                       |

Before changing isolation or lifecycle behavior, read the [sandbox limits](SANDBOX.md#supported-hosts-and-lifetime-limits) and [dependency trust boundary](REQUIREMENTS.md#host-resolution-and-trust).
