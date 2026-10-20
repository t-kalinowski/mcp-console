# Reference

Look up configuration keys, command-line options, language behavior, and the MCP, R, and Python APIs.
For a walkthrough or a task, start with the [guides](README.md).

<span id="find-a-task"></span>

The [“I want to…” table](README.md#task-index) connects tasks to their guides and reference pages.

## Configuration

[Configuration files](CONFIGURATION.md) explains file discovery, precedence, merging, and resolved settings.
Use the index below for a particular setting.

| Topic                                                                            | Reference                                                                            |
| -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| <span id="top-level-settings"></span> Top-level settings                         | [Top-level settings](CONFIGURATION.md#settings-by-task)                              |
| <span id="r"></span> R installation and startup                                  | [R installation and startup](RUNTIME_CONFIGURATION.md#r-executable-selection)        |
| <span id="python"></span> Python selection                                       | [Python selection](RUNTIME_CONFIGURATION.md#python-environment-selection)            |
| <span id="managed-python"></span> Managed Python                                 | [Managed Python](RUNTIME_CONFIGURATION.md#managed-python)                            |
| <span id="first-available-python"></span> Python fallback choices                | [Python fallback choices](RUNTIME_CONFIGURATION.md#first-available-python)           |
| <span id="resolution-policies"></span> Resolution policies                       | [Resolution policies](RUNTIME_CONFIGURATION.md#dependency-resolution-policies)       |
| <span id="session-startup"></span> SQL connection startup                        | [SQL connection startup](RUNTIME_CONFIGURATION.md#session-startup-source)            |
| <span id="environment"></span> Environment settings                              | [Environment settings](ENVIRONMENT.md#where-to-set-variables)                        |
| <span id="caches"></span> Caches                                                 | [Caches](RESOLVER.md#cache-locations)                                                |
| <span id="sandbox"></span> Sandbox settings                                      | [Sandbox settings](SANDBOX_CONFIGURATION.md)                                         |
| <span id="workspace-profile"></span> Workspace profile                           | [Workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile)                      |
| <span id="temporary-storage"></span> Temporary storage                           | [Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage)                      |
| <span id="filesystem"></span> Filesystem permissions                             | [Filesystem permissions](SANDBOX_CONFIGURATION.md#filesystem)                        |
| <span id="network-access"></span> Network access                                 | [Network access](SANDBOX_CONFIGURATION.md#network-choices)                           |
| <span id="proxy-settings"></span> Proxy settings                                 | [Proxy settings](SANDBOX_CONFIGURATION.md#proxy-modes-and-destinations)              |
| <span id="destination-rules"></span> Destination rules                           | [Destination rules](SANDBOX_CONFIGURATION.md#destination-rules)                      |
| <span id="local-sockets-and-binding"></span> Local sockets and binding           | [Local sockets and binding](SANDBOX_CONFIGURATION.md#unix-sockets-and-local-binding) |
| <span id="package-preparation"></span> Package preparation                       | [Package preparation](RESOLVER.md#configuration)                                     |
| <span id="languages"></span> Language visibility                                 | [Language visibility](TOOL_CONFIGURATION.md#model-visible-languages)                 |
| <span id="tool-presentation"></span> Tool presentation                           | [Tool presentation](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters)          |
| <span id="parameter-settings"></span> Parameter settings                         | [Parameter settings](TOOL_CONFIGURATION.md#parameter-settings)                       |
| <span id="null-empty-values-and-merging"></span> Null, empty values, and merging | [Null, empty values, and merging](CONFIGURATION.md#merge-rules)                      |

## Language behavior

[Language behavior](RUNTIME_REFERENCE.md) specifies R and Python helpers, SQL connections and results, plotting, and output handling.
For an introduction, read [Sessions and languages](BUILTIN_RUNTIME.md).

## Command line

- [Command-line reference](CLI.md) documents command behavior, options, and installation checks.
- [CLI help](cli-help.qmd) contains the public commands' complete help text.

<span id="apis-and-package-manuals"></span>

## MCP

- [MCP send reference](API.md) documents tool arguments, results, and output limits.
- [Calls, input, and control](SEND_OPERATIONS.md) covers polling, interactive input, interruption, and restart.
- [Requirements API](REQUIREMENTS_API.md) covers dependency actions.

## Packages

- The [Python API](https://t-kalinowski.github.io/mcp-console/python/reference/) documents synchronous and asynchronous clients and model integrations.
  The [Python package overview](https://t-kalinowski.github.io/mcp-console/python/) links to installation, first calls, and framework integration guides.
- The [R API](https://t-kalinowski.github.io/mcp-console/r/reference/) documents tool registration and session configuration.
  The [R package manual](../r/README.md) includes configuration and analysis examples.

## Glossary

The [glossary](GLOSSARY.md) defines terms used throughout Console's documentation.
