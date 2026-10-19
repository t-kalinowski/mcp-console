---
filters:
  - _filters/config-init.py
---

# Configuration files

Console works without a configuration file.
Use `mcp-console config init` to generate a starter file with commented examples, then edit it to choose interpreters, packages, and permissions.
The [settings index](#settings-by-task) links to the accepted values, defaults, and behavior for each topic.

Configuration is trusted launcher input: it can select executables, supply credentials, and widen permissions.
[File precedence](#files-and-precedence) explains automatic project loading and how to skip it.

## Create a configuration file

Run this command in your project directory:

```sh
mcp-console config init
```

By default, `config init` creates `.agents/console/config.yaml` in the current directory, including outside Git.
It does not search parent directories.
`--directory PATH` selects an existing project directory and interprets relative settings there.
`--global` creates `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
The two options are mutually exclusive.
These files are shared by clients that launch Console with the same configuration locations.
Client registration remains a separate operation; see [Connect your client](getting-started.qmd#connect-your-client).

The starter selects `sandbox.profile: workspace`: ordinary workspace writes are allowed, while the project's `.git`, `.agents`, `.codex`, and `.aws` paths stay read-only.
Other readable host files remain readable.
It includes commented examples for permissions, packages, interpreters, environment variables, and caches.
Lines beginning with `##` are explanations; lines beginning with `#` are YAML examples to uncomment.

Use repeated `-c` or `--config` assignments to override the starter settings, with the same ordering and merge rules as launch overrides.
Assignments may appear before `config` or after `init`.
Supplied sections show active YAML with their guidance; unconfigured sections retain commented examples.
To start with read-only permissions instead, replace the starter profile:

```sh
mcp-console config init -c sandbox.profile=read_only
```

Later profile overrides replace the starter profile without retaining explicit workspace write grants.
Filesystem settings add exceptions to the selected profile; to restrict writes to selected directories, choose `read_only` and supply those directories in `sandbox.filesystem.read_write`.

Only the starter permissions and supplied settings are saved: initialization does not copy discovered global or project configuration or expand built-in defaults.
Explicit values are retained even when they equal built-in defaults, so a new project layer can override different global values.
Configuration-source flags such as `--config-file` and `--no-config` are not accepted.
`--default-config` is a launch setting and is not accepted by initialization; use `-c` for settings to save in the new file.

The command validates settings without starting interpreters or preparing packages, creates the configuration directory if needed, and reports the file path and contents.
An existing destination causes an error and is left untouched, including with `--dry-run` or `-c`.
`--dry-run` prints the proposed path and contents without creating files or directories.

### Generated configuration

With no overrides, `config init` generates this `config.yaml`:

::: {.config-init-template}
[View the generated configuration](../tests/snapshots/cli/interface/test_config_init/dry_run_previews_the_created_contents.yaml).
:::

## Settings by task {#settings-by-task}

<span id="top-level-settings"></span>

Configuration must be a YAML or JSON mapping.
Unknown fields are errors.
Each reference below owns the complete settings and behavior for its topic; dotted paths such as `r.packages` show nesting.

| Setting                              | Value or form                                           | Reference                                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`                                  | Executable path, mapping, or `null`                     | <span id="r-executable-selection"></span> <span id="native-r-startup"></span> [Select R and its startup behavior](RUNTIME_CONFIGURATION.md#r-executable-selection). |
| `python`                             | Executable/venv path, selection mapping, or `null`      | <span id="python-environment-selection"></span> [Select an existing or managed Python environment](RUNTIME_CONFIGURATION.md#python-environment-selection).          |
| `r.packages`, `python.packages`      | Lists within the runtime mapping                        | <span id="startup-package-declarations"></span> [Choose startup packages](RUNTIME_CONFIGURATION.md#startup-package-declarations).                                   |
| `r.resolution`, `python.resolution`  | Policy names within the runtime mapping                 | <span id="dependency-resolution-policies"></span> [Control package preparation](RUNTIME_CONFIGURATION.md#dependency-resolution-policies).                           |
| `startup`                            | Mapping or `null`                                       | <span id="session-startup-source"></span> [Select a database connection at startup](RUNTIME_CONFIGURATION.md#session-startup-source).                               |
| `sandbox`                            | Mapping or `null`                                       | [Set filesystem and network permissions](SANDBOX_CONFIGURATION.md).                                                                                                 |
| `resolver`                           | Mapping                                                 | [Configure package sources and preparation](RESOLVER.md#configuration).                                                                                             |
| `cache`                              | `console`, `host`, or `null`                            | <span id="cache-selection"></span> [Choose package cache locations](RESOLVER.md#cache-locations).                                                                   |
| `inherit_environment`, `environment` | Boolean; mapping of names to strings                    | <span id="shared-environment"></span> [Set environment values and inheritance](ENVIRONMENT.md#where-to-set-variables).                                              |
| `languages`                          | Nonempty list of `r`, `python`, and/or `sql`; or `null` | <span id="model-visible-languages"></span> [Choose the languages exposed to the agent](TOOL_CONFIGURATION.md#model-visible-languages).                              |
| `tool`                               | Mapping                                                 | <span id="tool-descriptions-and-parameters"></span> [Customize tool descriptions and parameters](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters).           |

For the underlying concepts, read [Packages and environments](REQUIREMENTS.md) and [Sandbox and trust](SANDBOX.md).
For launch flags, see the [Command-line reference](CLI.md).

## Files and precedence

`serve` and ordinary `sandbox` launches read, in order:

1. Each `--default-config KEY=VALUE` assignment, in argument order.
2. Global `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
3. Project `.agents/console/config.yaml` in the launch directory.
   No ancestor search is performed.
4. Each `-c KEY=VALUE` override, in argument order.

Missing automatic files are normal.
Selected files must be readable YAML mappings; invalid files fail launch.
`--config-file PATH` instead selects one required YAML or JSON mapping and skips both automatic locations.
Launch defaults still apply beneath the selected file, and CLI overrides apply afterward.
Discovery exclusions skip files, not `--default-config` or `-c` assignments.

| Option                | Automatic files read |
| --------------------- | -------------------- |
| No option             | Global, then project |
| `--no-project-config` | Global only          |
| `--no-global-config`  | Project only         |
| `--no-config`         | Neither              |

Discovery exclusions cannot accompany `--config-file`.
Options may appear before or after the subcommand.
They do not edit files or change recording-location selection.

**Project configuration is trusted launcher input.** It can select executables, supply credentials, and widen sandbox permissions.
Console loads this layer automatically without a separate approval step, whether or not the directory is in Git and including with global client registration.
Review and authorize these settings before launching Console; `--no-project-config` skips the project layer for unfamiliar projects.
Global settings and launch defaults, including inherited client permissions, are not an enforcement ceiling: project settings can replace the selected profile or add permissions.

Settings are captured when the server starts.
Editing YAML requires a new server connection; restarting a worker does not reload it.

## Inspect and edit resolved configuration

Inspect the settings Console would use before starting a session:

```sh
mcp-console config resolve
mcp-console config resolve --full > session.yaml
# Review session.yaml, then launch with that file alone.
mcp-console serve --config-file session.yaml
```

`config resolve` uses the same file selection, ordered `-c` overrides, and validation as `serve`.
It does not start interpreters, prepare packages, or create session/cache directories.
Use `--no-config` to skip discovered files, `--config-file PATH` for one required file, and `--format json` for JSON instead of YAML.
To inspect the built-in defaults without launch overrides, run `mcp-console config resolve --no-config --full`.

The default `--prune` omits values equal to the binary's built-in defaults, independently of discovered files.
The comparison uses defaults for the same sandbox choice as the export.
An unchanged configuration produces `{}`.
Mappings are pruned recursively.
Changed arrays and scalar values stay complete, including empty arrays and strings; overrides needed to preserve conditional defaults remain explicit.
`--full` includes defaults and generated tool descriptions; it cannot be combined with `--prune`.
See [Tool configuration](TOOL_CONFIGURATION.md#inspect-and-edit-resolved-configuration) before editing that generated text or schema metadata.

Use the same launch directory for export and execution so relative paths retain their meaning.
For host execution, pass `--no-sandbox` to both commands; launch flags are not stored in YAML.
Runtime-dependent interpreter selection, automatic package lists, inherited environment values, and runtime-created cache and temporary paths remain policies.
Python `first_available` retains its candidates, including `active_venv`; the next launch evaluates them against its own environment and filesystem.
Managed fallback entries include their effective resolution policy even when an existing candidate is selected.
An export does not pin installed dependencies or copy the host environment.
Exported interpreter paths must be representable as UTF-8.

R exposes the same operations through its [configuration workflow](https://t-kalinowski.github.io/mcp-console/r/reference/config-workflow.html).

## Command-line overrides

```sh
mcp-console serve -c 'sandbox.filesystem.read_write=[.]'
mcp-console serve --no-project-config -c 'python=.venv'
mcp-console serve --config-file ./session.yaml
```

Use `--default-config KEY=VALUE` to supply a preference that configuration files can override:

```sh
mcp-console serve --default-config sandbox.profile=inherit_codex
mcp-console config resolve --default-config sandbox.profile=inherit_codex
```

Both assignment options accept the same dotted keys and YAML values.
Repeated assignments preserve order within their layer, including assignments before and after the subcommand.
Files always take precedence over `--default-config`, and `-c` always takes precedence over files, regardless of argument placement.
Explicit `null` and empty lists retain their normal replacement semantics; they are not treated as missing values.

Dotted keys address mappings, not list indexes.
Values accept YAML strings, booleans, finite numbers, lists, objects, and `null`.
Quote the assignment for the shell and quote strings that resemble numbers or booleans.
To set a literal dotted environment name, supply its containing mapping:

```sh
mcp-console serve -c 'environment={"APP.VERSION": "v1", COUNT: "42"}'
```

There is no variable expansion or file inclusion in these values.
`-c` accepts an assignment, not a file path or root configuration document.
The separate native `sandbox --config-env` interface cannot be combined with application configuration options; see [CLI](CLI.md).

## Merge rules

Mappings merge recursively.
Lists, scalars, and `null` replace the earlier value.
Validation and defaults apply to the final merged settings.

| Value         | Effect                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------- |
| Omitted field | Keep the earlier layer's value, or use the default when no layer supplies one.                                   |
| `{}`          | Merge an empty mapping; do not clear earlier entries. Runtime shorthands are expanded first, as described below. |
| `[]`          | Replace a list with an empty list, where allowed. `languages` and `python.first_available` must remain nonempty. |
| `null`        | Replace the earlier value, where allowed. Clearing `sandbox` or `resolver.sandbox` restores the policy defaults. |
| `""`          | Supply an empty string. This is valid for environment values and tool prose; it does not unset a variable.       |

For example, global `environment: {KEEP: global, CHANGE: global}` and project `environment: {CHANGE: project}` retain `KEEP` and replace `CHANGE`.

To replace a whole mapping, clear it and then supply the replacement through ordered overrides:

```sh
mcp-console serve -c sandbox.network=null -c 'sandbox.network={proxy: {mode: limited}}'
```

The final value must still be valid for that setting.
For example, `sandbox.filesystem`, `sandbox.network.proxy`, `environment`, and `resolver` require mappings rather than `null`.
Changing one selector does not implicitly remove incompatible siblings.

Before merging each layer, Console expands an R path to `r.executable` and direct managed Python fields to `python.managed`.
An empty `python: {}` also selects the managed form.
Flat and nested runtime settings therefore merge at the same paths; [Interpreters and startup](RUNTIME_CONFIGURATION.md) describes the selection rules.

## Paths

Relative paths use the launch directory, including paths written in a global file.
A leading `~` expands to the launching user's home in interpreter selectors, sandbox and resolver filesystem paths, supported Unix-socket paths, configuration filenames, and `--writable-root`.
`MCP_CONSOLE_HOME` also accepts a leading `~`.

Expansion uses a nonempty, absolute `HOME` from the launch environment.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-profile discovery.
`R_USER`, `MCP_CONSOLE_HOME`, and child `environment` or `resolver.environment` overrides do not redefine that home.
An invalid home is an error when expansion is needed.

Only `~`, `~/...`, and, on Windows, `~\...` expand.
Quote a standalone `"~"` in YAML, where an unquoted `~` means `null`.
Use `./~/...` to name a literal `~` directory.
`~user`, globs, and environment-variable references are not expanded.
Expansion preserves symlinks and does not create directories.
Settings expand after file and CLI layering; configuration filenames expand before reading.
Materialized settings expose expanded home paths, including paths in Python candidate lists.
Other strings, including environment values and startup programs, retain their contents.

## Console home

Set `MCP_CONSOLE_HOME` in the launching environment to a directory to relocate global configuration and fallback session storage.
It does not relocate package caches, change `HOME`, or override project-local recording selection.
The directory must be absolute after leading-`~` expansion; empty or relative values are errors when it is needed.

Without it, Console uses an absolute `HOME`.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-home discovery.
See [Recordings](RECORDING.md) for project-local storage.

## Migration

Global configuration now contributes defaults even when a project file exists.
Use discovery exclusions to opt out.
Older native-shaped YAML, `extends`, and camelCase permission fields are not accepted.
Application profiles select workspace permissions or inherited client permissions with an explicit fallback; they are separate from complete native policy selectors.
Use the public [sandbox schema](SANDBOX_CONFIGURATION.md); complete native JSON remains a separate explicit interface.
