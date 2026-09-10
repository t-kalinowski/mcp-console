# Sandbox configuration

Select a policy explicitly in the trusted process that launches the sandbox:

```sh
SANDBOX_POLICY='{"version":2,"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

`SANDBOX_POLICY` contains JSON, not a filename.
Console forwards the selected name to its verified private runner, which owns the configuration types, validation, and enforcement.
An explicit configuration supplies the complete policy; Console does not merge its default policy or macOS extension into it.

Without `--config-env`, `sandbox` uses the [Console defaults](SANDBOX.md#application-policy-and-launch), augmented by any explicit writable roots.
`serve` supplies the same policy and never selects a policy from ambient environment state.
Setting `MCP_CONSOLE_SANDBOX_CONFIG` alone does not change either command's policy.

## Additional writable directories

`serve` and `sandbox` accept repeatable `--writable-root DIR` arguments:

```sh
mcp-console serve --writable-root './output files' --writable-root /path/to/cache
```

This temporary argument does not define the eventual configuration interface.
Each path must name an existing directory; Console does not create it.
Paths must be valid UTF-8 to fit the runner's configuration transport.
Relative paths resolve against the launch working directory before workload startup, and the server retains the absolute paths across worker restarts and replacements.
Paths remain separate arguments, including spaces and Unicode; symlink components remain subject to the runner's native writable-root validation.

The directories augment the default filesystem policy for the workload and its subprocesses.
Their parents, the working directory, and home receive no implicit write grant.
The runner's native safeguards, network restrictions, macOS extensions, private temporary storage, and lifecycle cleanup still apply.
These directories contain persistent user data; retirement removes only runner-owned private storage.
Omitting the argument preserves the default permissions.

`serve --no-sandbox` and `sandbox --config-env` each conflict with `--writable-root`.
An explicit `--config-env` value supplies a complete policy; no merging or precedence is defined.
There is no environment-variable interface for the directory list or automatic configuration lookup.
The launch code accepts a path list independently of argument parsing so a later configuration front end can supply the same list.

## Command and environment

Put launcher options before the command and use `--` to mark the command boundary.
The existing delimiter-free form also accepts a command as the first positional argument.
Everything after the command begins is a target argument, including option-looking values such as `--config-env`.
No shell is inserted; executable names and arguments are passed separately.
Select the working directory through the caller's process-launch API.
The configuration cannot contain `command` or `cwd`.

The target inherits the launch environment by default, excluding configuration transport variables.
An optional `environment` map supplies target overrides without serializing the rest of the caller's environment:

```json
{
  "version": 2,
  "filesystem": {
    "kind": "restricted",
    "entries": [
      {
        "path": { "type": "special", "value": { "kind": "root" } },
        "access": "read"
      },
      {
        "path": { "type": "path", "path": "/absolute/output" },
        "access": "write"
      }
    ]
  },
  "network": "restricted",
  "environment": { "MODE": "analysis" },
  "lifecycle": { "private_tmp": { "environment": ["TMPDIR"] } }
}
```

Set `inherit_environment` to `false` to start with an empty target environment and use `environment` as the complete ordinary target map.
The runner's private-directory exports and managed proxy values take precedence over ordinary target variables.
Linux supplies `PWD` from the working directory if the target map omits it.
Native runtime additions, such as macOS locale metadata, follow the host runtime's behavior.

Target overrides take effect only after helper setup and native enforcement.
They cannot select host helpers through `PATH`, relocate host setup through `TMPDIR`, inject code into host loaders, or configure host proxy/control state.
The trusted launch environment remains responsible for loading the frontend and runner and selecting host helpers.
Put target loader settings in the JSON `environment` map.
Use a dedicated transport name rather than a loader variable.

The selected name and reserved `MCP_CONSOLE_SANDBOX_CONFIG` are stripped from every helper and target environment, including explicit maps that try to reintroduce them.
Neither may also be a private-directory export.
`MCP_CONSOLE_SANDBOX` is an ordinary target marker, not an authorization check.

## Configuration fields and defaults

The pinned [runner protocol](https://github.com/t-kalinowski/codex/blob/7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc/codex-rs/mcp-console-sandbox/PROTOCOL.md) defines the canonical schema.
Its filesystem and proxy fields use the upstream types and validators directly.

| Field                              | Environment-mode contract                                                                                                                                                            |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `version`                          | Required integer, currently `2`.                                                                                                                                                     |
| `filesystem`                       | Required runner filesystem policy. The example grants host reads and one writable directory. Linux supervised execution requires a restricted filesystem policy.                     |
| `network`                          | Required: `"restricted"` or `"enabled"`.                                                                                                                                             |
| `proxy`                            | Optional managed proxy configuration; omitted or `null` means none. A supplied configuration must be enabled. Its allowlist is selected by the trusted launcher.                     |
| `macos_seatbelt_profile_extension` | Optional trusted SBPL appended to the native profile. It can grant permissions as well as restrict them. Omitted or `null` adds no extension; a supplied value is rejected on Linux. |
| `inherit_environment`              | Optional Boolean, default `true`.                                                                                                                                                    |
| `environment`                      | Optional target override map, default empty. Keys must be nonempty and contain neither `=` nor NUL; values must be NUL-free.                                                         |
| `linux_backend`                    | Optional on Linux: `"bubblewrap"` (default) or explicit `"landlock"`. Rejected on macOS.                                                                                             |
| `lifecycle`                        | Optional object with the defaults below.                                                                                                                                             |

| Lifecycle field      | Default and behavior                                                                                                                                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parent_pid`         | Omitted/`null`: no caller-death observation. Otherwise it must identify the runner's actual current parent and be greater than 1.                                                                                                                                           |
| `sigterm`            | `"forward"`: forward SIGTERM and preserve the target's exit status. `"retire"`: retire the sandbox on SIGTERM.                                                                                                                                                              |
| `private_tmp`        | Omitted/`null`: create no private storage. Otherwise provide `environment`, an array of export names, and optionally an absolute `parent` directory. The default parent is the supervisor's native temporary directory. The runner creates and removes its private storage. |
| `cleanup_timeout_ms` | `1000`; an explicit value must be between 1 and 60000. A retirement failure returns a nonzero status.                                                                                                                                                                       |

These are the runner defaults for an explicit configuration.
Console's no-config path additionally requests its macOS extension and private `TMPDIR`.
For default bubblewrap and macOS execution, descendant retirement and the [supported lifetime boundaries](SANDBOX.md#supported-hosts-and-lifetime-limits) still apply.

Repeated `--config-env`, combining it with private `--exit-with-parent`, duplicate top-level JSON fields, and unknown top-level fields are rejected.
Use `lifecycle.parent_pid` in an explicit configuration.
The runner rejects simultaneous environment and descriptor input options.
There is no configuration-file option, automatic file discovery, `@file` syntax, include, reload, or file fallback.

## Explicit Linux backend selection

`linux_backend: "bubblewrap"` retains the default namespace sandbox and supervised lifetime.
A namespace failure does not switch backends.
Native procfs fallback changes the procfs view while preserving the backend and policy; see [Linux compatibility](LINUX_COMPATIBILITY.md).

`linux_backend: "landlock"` explicitly selects native Landlock/seccomp enforcement and replaces the runner with the restricted command.
It needs no bubblewrap or namespace setup and preserves native policy-compatibility checks.
The direct native handoff uses a sealed anonymous memfd for target setup.
Restricted-read policies are unsupported.
A restricted filesystem requires Landlock truncate enforcement (ABI 3 or later); older best-effort enforcement is rejected.
Native device-ioctl restrictions depend on ABI 5 and are outside this backend's portable contract.

Landlock provides no process isolation: same-user host signalling may remain possible.
It provides no descendant retirement, caller-death cleanup, private storage, or terminal supervisor.
Accordingly, `parent_pid`, `private_tmp`, an explicit cleanup timeout, retirement SIGTERM, and managed proxy routing are rejected.
Ordinary exit and signal behavior follows direct native execution.
`serve` continues to request bubblewrap supervision.

## Caller examples

These runnable examples supply the environment only to the child and pass normal argument arrays.
They preserve spaces, Unicode, quotes, and shell-looking values:

```sh
sh examples/sandbox-config.sh /absolute/path/to/mcp-console
python3 examples/sandbox-config.py /absolute/path/to/mcp-console
Rscript examples/sandbox-config.R /absolute/path/to/mcp-console
```

The [shell example](../examples/sandbox-config.sh) uses command-local assignment and quoted positional parameters.
The [Python example](../examples/sandbox-config.py) passes a copied dictionary through `subprocess.run(env=...)`.
The [R example](../examples/sandbox-config.R) uses `processx::run(env=c("current", ...))` and requires `processx` and `jsonlite`.
No example temporarily changes a multithreaded parent's global environment.

## Integrity, stdin, and large requests

The OS copies the launch environment when the child is created.
The runner reads the selected value once into owned state and validates it before target execution.
Changing the parent's environment after successful launch, even before runner parsing, cannot change that copy.
The target cannot replace accepted policy by changing its own environment, creating configuration files, writing policy-looking output, or asking a nested sandbox for broader permissions.
Native restrictions remain in effect for descendants.

Removing a transport variable prevents propagation; it does not authenticate the caller or securely erase memory.
Under the tested restricted policy, the workload cannot read the host supervisor's launch environment, obtain its process-control resources, or inherit bootstrap, setup, or proxy-control descriptors.
The trusted launcher still owns the executable, initial loader environment, policy, and authority conveyed by stdio.
An unrestricted same-user host process lies outside this boundary.

JSON is capped at 1 MiB, but actual exec limits can be smaller and include arguments, the rest of the environment, and platform overhead.
Linux also limits individual argument/environment strings.
An oversized initial launch fails in the caller with `E2BIG`; the frontend cannot print a diagnostic before it starts.
Later exec failures report the OS error without dumping complete environments.
Malformed JSON diagnostics report category and location without echoing values.
Stdin always belongs to the target and is never read to discover configuration.

Large integrations may explicitly use the private runner's existing `--bootstrap-fd N` transport.
This remains a private installed-companion interface: it requires an inherited readable descriptor above stderr carrying a four-byte big-endian length followed by 1 through 1,048,576 bytes of JSON.
Descriptor-mode JSON also supplies `command`, absolute `cwd`, and a complete target `environment`; it rejects `inherit_environment`.
Start the runner before writing a frame larger than a pipe buffer.

Descriptor integrity begins at complete request acceptance, rather than at process creation.
The trusted writer must control the bytes and all writers until acceptance; the descriptor itself does not authenticate its source.
The runner consumes exactly one frame, closes the descriptor, and never rereads it.
Neither path spills policy to mutable files, and descriptor transport does not bypass the final target's exec limits.
