# Sandbox configuration

## Project configuration

`serve` and ordinary `sandbox` launches read either `.mcp-console/config.yaml` or `.agents/mcp-console.yaml` beneath the launch working directory.
Only that directory is searched: no ancestors, home directory, or global configuration.
If both files exist, the launch fails with an ambiguity error.
An absent file preserves the defaults; an unreadable or invalid existing file prevents launch.

Project configuration is trusted launcher input and can widen workload permissions.
Review it before launching Console in a project.
This first interface adds settings to Console's existing policy; it is not a complete runner policy or the final configuration API.

For example, create `output` in your project and put this in `.mcp-console/config.yaml`:

```yaml
sandbox:
  filesystem:
    kind: restricted
    entries:
      - path:
          type: path
          path: ./output
        access: write
  network: restricted
  proxy:
    enabled: true
    mode: full
    domains:
      example.com: allow
      blocked.example.com: deny
```

Then launch from the project directory:

```sh
mcp-console sandbox -- python3 -c 'from pathlib import Path; Path("output/result.txt").write_text("ready")'
mcp-console serve
```

Console retains its default host read access, private temporary storage, platform compatibility rules, and lifecycle cleanup.
Filesystem entries only add literal path write grants through the same path as `--writable-root`.
Configured grants and repeated CLI writable roots are additive.
Relative paths resolve against the launch working directory, not the metadata directory containing the YAML file.
Console does not create paths, grant their parents, expand `~` or environment variables, or canonicalize away symlink components.
The existing [native path behavior and platform limitations](#additional-writable-paths) apply.

| Field under `sandbox`      | Supported values and defaults                                                                                                                                                                                                                    |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `filesystem`               | Mapping, default empty.                                                                                                                                                                                                                          |
| `filesystem.kind`          | `restricted` only; may be omitted.                                                                                                                                                                                                               |
| `filesystem.entries`       | List, default empty. Every entry requires `path: {type: path, path: STRING}` and `access: write`. Other path forms and access modes are rejected.                                                                                                |
| `network`                  | `restricted` (default) or `enabled`. A proxy does not implicitly change this setting.                                                                                                                                                            |
| `proxy`                    | Omitted or `null`: no proxy. Otherwise a mapping with `enabled: true`.                                                                                                                                                                           |
| `proxy.enabled`            | Required Boolean `true` when a proxy object is supplied. To remove the proxy, omit the object or use `null`.                                                                                                                                     |
| `proxy.mode`               | `full` (default) or `limited`, using the native proxy's modes. `full` permits all HTTP methods and HTTPS CONNECT tunnels to allowed destinations. `limited` permits HTTP GET/HEAD/OPTIONS and blocks HTTPS CONNECT in this runner configuration. |
| `proxy.domains`            | Mapping from native domain patterns to `allow`, `deny`, or `none`. Omitted, `null`, and empty maps grant no destinations. `none` adds no permission; explicit denies take precedence over allows.                                                |
| `proxy.enableSocks5`       | Boolean, default `true`.                                                                                                                                                                                                                         |
| `proxy.allowUpstreamProxy` | Boolean, default `false`.                                                                                                                                                                                                                        |
| `proxy.allowLocalBinding`  | Boolean, default `false`; opts into the native local/private network and binding exceptions.                                                                                                                                                     |

Console passes domain patterns and permissions to the pinned runner without matching or rewriting them.
The native proxy owns host normalization, pattern matching, local-network checks, and enforcement.
For example, with local binding disabled, an explicitly allowlisted loopback IP literal can be reached through the proxy; a hostname resolving to a private address remains subject to the native local-network restriction.
An empty allowlist does not mean unrestricted proxy access.
When a proxy is supplied, the runner enforces managed proxy routing even with `network: enabled`; that value does not bypass the proxy.
The native exceptions selected by `allowLocalBinding` still apply.

Console supplies all required runner proxy fields explicitly.
SOCKS5 UDP, upstream-proxy use, local binding, and Unix-socket exceptions default to disabled; only the exposed switches above can change their corresponding options.
UDP and Unix-socket controls, listener/control internals, protocol versions, native backends, platform extensions, command, cwd, target environment, and lifecycle settings are not YAML fields.

Configuration must contain exactly one UTF-8 YAML 1.2 mapping document.
The `sandbox` mapping may be omitted; `{}` preserves defaults.
Console checks the supported application fields and additive filesystem entry forms, then delegates native policy types, values, and validation to the runner.
Unknown fields and malformed input are errors; custom tags are unsupported.
For duplicate keys, the last value wins because of a known limitation of Saphyr's node loader; this behavior is subject to change.
Booleans use YAML 1.2 values such as `true` and `false`; strings such as `yes` are not Boolean options.
There are no includes, merge keys, interpolation, shell expansion, layering, or reloads.
Parsing uses Saphyr's YAML node API in Rust and does not start Python or resolve a Python environment.
Scalar and tag resolution follow the pinned Saphyr loader.

The trusted outer process reads and normalizes configuration once, before workload startup.
When a project configuration file exists, it probes the native sandbox with a no-op process using those captured settings.
This checks native policy validation and sandbox/proxy startup before running the workload or announcing server readiness, without starting a worker.
Probe failures include the configuration filename and the runner's diagnostic; native JSON error locations refer to the generated runner policy.
`serve` retains that snapshot for the whole session, including when neither file existed at launch.
Editing, removing, or creating either file later cannot change the initial worker, a restart that resets its state, recovery after worker failure, or a replacement using newly prepared requirements.
Internal launches explicitly select the captured application settings through a child-specific environment payload and the public `sandbox` launch boundary; no child rereads a filename.
The sandbox layer constructs native policy and strips the private settings transport before launching the runner and workload.
Ambient `MCP_CONSOLE_SANDBOX_SETTINGS` or `MCP_CONSOLE_SANDBOX_CONFIG` values do not select policy, and the server's global environment is not modified.

`serve --no-sandbox` bypasses sandbox configuration entirely.
Explicit `sandbox --config-env NAME` also bypasses discovery and retains the complete-policy interface below.
Both still conflict with explicit `--writable-root` arguments.

## Explicit complete policy

Select a policy explicitly in the trusted process that launches the sandbox:

```sh
SANDBOX_POLICY='{"version":2,"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

`SANDBOX_POLICY` contains JSON, not a filename.
Console forwards the selected name to its verified private runner, which owns the configuration types, validation, and enforcement.
An explicit configuration supplies the complete policy; Console does not merge its default policy or macOS extension into it.

Without `--config-env`, `sandbox` uses the [Console defaults](SANDBOX.md#application-policy-and-launch), augmented by discovered project settings and explicit writable roots.
`serve` supplies the same policy and never selects a policy from ambient environment state.
Setting `MCP_CONSOLE_SANDBOX_CONFIG` alone does not change either command's policy.

## Additional writable paths

`serve` and `sandbox` accept repeatable `--writable-root PATH` arguments:

```sh
mcp-console serve --writable-root './output files' --writable-root /path/to/cache
```

This temporary argument does not define the eventual configuration interface.
Paths may name directories, individual files, or locations that do not exist yet.
Console does not inspect, create, or remove them; it leaves filesystem handling to the runner.
Paths must be valid UTF-8 to fit the runner's configuration transport.
Relative paths resolve against the launch working directory before workload startup, and the server retains the absolute paths across worker restarts and replacements.
Paths retain spaces and Unicode; symlink components remain subject to the runner's native writable-root validation.

The paths augment the default filesystem policy for the workload and its subprocesses.
Their parents, the working directory, and home receive no implicit write grant.
The runner's native safeguards, network restrictions, macOS extensions, private temporary storage, and lifecycle cleanup still apply.
These paths contain persistent user data; retirement removes only runner-owned private storage.
Omitting both CLI and configured grants preserves the default filesystem permissions.

On macOS, a missing path can be created by the workload under the native path rule.
On Linux, the runner skips paths absent at sandbox startup; a directory created later becomes writable on a new sandbox launch, such as a worker restart.
macOS supports individual file grants; the pinned Linux runner currently fails on existing file roots while preparing its directory metadata protections.
Console does not substitute a parent-directory grant or change backends for these paths.

`serve --no-sandbox` and `sandbox --config-env` each conflict with `--writable-root`.
An explicit `--config-env` value supplies a complete policy; no merging or precedence is defined.
There is no ambient environment-variable interface for the path list.
Discovered filesystem entries supply the same additional-writable-root list.

## Command and environment

Put launcher options before the command and use `--` to mark the command boundary.
The existing delimiter-free form also accepts a command as the first positional argument.
Everything after the command begins is a target argument, including option-looking values such as `--config-env`.
No shell is inserted; executable names and arguments are passed separately.
Select the working directory through the caller's process-launch API.
The configuration cannot contain `command` or `cwd`.

With the explicit complete-policy interface, the target inherits the launch environment by default, excluding configuration transport variables.
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

The pinned [runner protocol](https://github.com/t-kalinowski/codex/blob/689f48c30deeb6aaa95a193e31e5971dd6820465/codex-rs/mcp-console-sandbox/PROTOCOL.md) defines the canonical schema.
Its filesystem and proxy fields use the upstream types and validators directly.

| Field                              | Environment-mode contract                                                                                                                                                                                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`                          | Required integer, currently `2`.                                                                                                                                                                                                                              |
| `filesystem`                       | Required runner filesystem policy. The example grants host reads and one writable directory. Supervised Linux rejects policies classified as full-filesystem write access; see [filesystem classification](LINUX_COMPATIBILITY.md#filesystem-classification). |
| `network`                          | Required: `"restricted"` or `"enabled"`. Enabling networking does not grant filesystem writes.                                                                                                                                                                |
| `proxy`                            | Optional managed proxy configuration; omitted or `null` means none. A supplied configuration must be enabled. Its allowlist is selected by the trusted launcher.                                                                                              |
| `macos_seatbelt_profile_extension` | Optional trusted SBPL appended to the native profile. It can grant permissions as well as restrict them. Omitted or `null` adds no extension; a supplied value is rejected on Linux.                                                                          |
| `inherit_environment`              | Optional Boolean, default `true`.                                                                                                                                                                                                                             |
| `environment`                      | Optional target override map, default empty. Keys must be nonempty and contain neither `=` nor NUL; values must be NUL-free.                                                                                                                                  |
| `linux_backend`                    | Optional on Linux: `"bubblewrap"` (default) or explicit `"landlock"`. Rejected on macOS.                                                                                                                                                                      |
| `lifecycle`                        | Optional object with the defaults below.                                                                                                                                                                                                                      |

| Lifecycle field      | Default and behavior                                                                                                                                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parent_pid`         | Omitted/`null`: no caller-death observation. Otherwise it must identify the runner's actual current parent and be greater than 1.                                                                                                                                           |
| `sigterm`            | `"forward"`: forward SIGTERM and preserve the target's exit status. `"retire"`: retire the sandbox on SIGTERM.                                                                                                                                                              |
| `private_tmp`        | Omitted/`null`: create no private storage. Otherwise provide `environment`, an array of export names, and optionally an absolute `parent` directory. The default parent is the supervisor's native temporary directory. The runner creates and removes its private storage. |
| `cleanup_timeout_ms` | Omitted/`null`: `1000` for supervised execution; an explicit value must be between 1 and 60000. A retirement failure returns a nonzero status.                                                                                                                              |

These are the runner defaults for an explicit configuration.
Console's no-config path additionally requests its macOS extension and private `TMPDIR`.
For default bubblewrap and macOS execution, descendant retirement and the [supported lifetime boundaries](SANDBOX.md#supported-hosts-and-lifetime-limits) still apply.

Repeated `--config-env`, combining it with private `--exit-with-parent`, duplicate top-level JSON fields, and unknown top-level fields are rejected.
Use `lifecycle.parent_pid` in an explicit configuration.
The runner rejects simultaneous environment and descriptor input options.
The explicit interface has no configuration-file option, discovery, `@file` syntax, include, reload, or file fallback.

## Explicit Linux backend selection

Omitting `linux_backend` or selecting `"bubblewrap"` retains the default namespace sandbox and supervised lifetime, even when `lifecycle` is omitted.
Both reject unrestricted filesystem policies and special root write grants without effective narrower rules, with either network setting.
Root write with effective read-only or denied carveouts proceeds to native validation; it is not equivalent to unrestricted access.
See the [exact classification and remaining supervision restriction](LINUX_COMPATIBILITY.md#filesystem-classification).
A namespace or policy failure does not switch backends or retry the target unsandboxed.
Native procfs fallback changes the procfs view while preserving the backend and policy; see [Linux compatibility](LINUX_COMPATIBILITY.md).

`linux_backend: "landlock"` explicitly selects native Landlock/seccomp enforcement and replaces the runner with the restricted command.
It needs no bubblewrap or namespace setup and preserves native policy-compatibility checks.
The direct native handoff uses a sealed anonymous memfd for target setup.
Root-readable policies and compatible writable-directory grants retain native Landlock enforcement.
Restricted-read policies, including root read with denied carveouts, are unsupported; native validation also rejects policies its legacy representation cannot preserve, such as root write with carveouts.
When the policy is not classified as full-filesystem write access, the runner requires Landlock truncate enforcement (ABI 3 or later); older best-effort enforcement is rejected.
An explicitly full-write policy skips filesystem Landlock enforcement and this ABI requirement; requested network restrictions still apply.
Native device-ioctl restrictions depend on ABI 5 and are outside this backend's portable contract.

Landlock provides no process isolation: same-user host signalling may remain possible.
It provides no descendant retirement, caller-death cleanup, private storage, or terminal supervisor.
Accordingly, `parent_pid`, `private_tmp`, an explicit cleanup timeout, retirement SIGTERM, and managed proxy routing are rejected.
The runner validates those incompatible requests before native setup:

| Option               | Accepted with Landlock                            | Rejected with Landlock                                       |
| -------------------- | ------------------------------------------------- | ------------------------------------------------------------ |
| `lifecycle`          | Omitted or `{}`                                   | Requests for the supervised features below                   |
| `parent_pid`         | Omitted or `null`                                 | A supplied PID, including the actual caller                  |
| `private_tmp`        | Omitted or `null`                                 | Any supplied object, including an empty export list          |
| `sigterm`            | Omitted or explicit `"forward"`                   | `"retire"`                                                   |
| `cleanup_timeout_ms` | Omitted or `null`; no retirement deadline applies | Any supplied number, including the supervised default `1000` |
| `proxy`              | Omitted or `null`                                 | A supplied managed proxy configuration                       |

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
