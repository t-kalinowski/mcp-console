# Sandbox configuration

## Project configuration

`serve` and ordinary `sandbox` launches read only `.agents/console/config.yaml` beneath the launch working directory.
Only that directory is searched: no ancestors, home directory, or global configuration.
An absent file preserves the defaults; an unreadable or invalid existing file prevents launch.

Project configuration is trusted launcher input and can widen workload permissions.
Review it before launching Console in a project.
Console forwards native sandbox fields and values to the runner, which owns their validation and defaults.
Console supplies only its application policy and launch requirements.

For project editing, put this complete configuration in `.agents/console/config.yaml`:

```yaml
extends: ":workspace"
```

`":workspace"` grants workspace write access with protected metadata paths: `.git`, `.agents`, `.codex`, and `.claude` beneath the fixed launch working directory are readable but protected from writes by default, including from subprocesses.
Protecting `.agents` also covers `.agents/console/config.yaml` and Console's managed state.
The colon is part of the string and reserves the name for a native built-in.
Quote these identifiers in configuration examples; valid unquoted YAML such as `extends: :workspace` has the same meaning through the ordinary YAML parser.

To select the native read-only baseline explicitly:

```yaml
extends: ":read-only"
```

Omitting `extends` preserves Console's existing defaults.
No version field, named user profiles, inheritance chains, alternate discovery locations, initializer, or CLI profile selector are implemented.
Unsupported identifiers reach the native runner and receive its diagnostic.

Console reuses the native constructors and workspace materialization, including their Git pointer, symlink, and missing-path handling.
It supplies `.claude` as an ordinary native `read` entry; the constructor already supplies the other metadata defaults.
Networking remains restricted by default.
For `":workspace"`, Console explicitly sets both `workspace_options.exclude_tmpdir_env_var` and `workspace_options.exclude_slash_tmp` to `true`, omitting inherited `TMPDIR` and shared `/tmp` write grants while retaining the runner's private writable temporary storage.
These options may be supplied under `sandbox.workspace_options`; explicit values pass through, with omitted keys defaulting to `true` in Console.
An explicit `null` options object selects the runner's native defaults, which include both temporary-directory grants.

The built-in supplies the baseline; explicit restricted filesystem entries augment it, and explicit networking replaces its network setting.
An empty restricted entry array does not erase the baseline.
Explicit `unrestricted` or `external-sandbox` filesystem kinds replace the baseline filesystem and retain their native enforcement meaning.
Console does not generate a replacement host-read or network policy for a selected built-in.

Metadata protections are defaults, not mandatory write-denial ceilings.
Native specificity applies: a more specific entry overrides an ancestor, and equal paths use `deny` over `write` over `read`, independently of array order.
For example, this deliberate exception permits writes under `.claude`:

```yaml
extends: ":workspace"
sandbox:
  filesystem:
    entries:
      - path: {type: path, path: .claude}
        access: write
```

A native `read` entry grants access as well as excluding writes from a broader write grant.
It can reopen reads beneath a broader read denial; more specific denials still apply in the native model.
The current Linux mount backend can instead hide that narrower read grant, and an additional nested denial can fail at mount setup.
macOS honors the narrower read grant in these cases.
Console preserves these native outcomes and diagnostics; it does not add a path matcher or switch backends.
Native Linux setup may use temporary mount placeholders for absent protected paths; Console creates no directories to apply policy, and those paths remain absent after retirement.
See [native path behavior](#additional-writable-paths) for other platform limits.

For more selective writes and a managed network proxy, create `output` and use:

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
    enableSocks5: true
    enableSocks5Udp: false
    allowUpstreamProxy: false
    dangerouslyAllowAllUnixSockets: false
    allowLocalBinding: false
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

For an omitted filesystem kind, `kind: restricted`, or the equivalent native representation `kind: {restricted: null}`, Console adds configured filesystem entries and repeated CLI writable roots to the selected baseline, or to its default host read grant when no built-in is selected.
Other filesystem kinds are forwarded without that grant or the default macOS extension.
For example, `sandbox: {filesystem: {kind: unrestricted}}` requests unrestricted filesystem access, and `sandbox: {filesystem: {kind: external-sandbox}}` delegates enforcement to an outer sandbox.
The `send` tool description reflects the selected filesystem and network access.
See [enforcement modes](#filesystem-and-enforcement-modes).

Console resolves literal filesystem paths (`path: {type: path, path: STRING}`) against the launch working directory, not the metadata directory containing the YAML file.
It does not create paths, grant their parents, expand `~` or environment variables, or canonicalize away symlink components.
Other path forms, access modes, filesystem fields, network representations, proxy fields, and native options pass through without a Console allowlist.
The runner decides whether to accept them, including unknown values and fields.
The existing [native path behavior and platform limitations](#additional-writable-paths) apply.

Without `extends`, Console supplies `network: restricted` when omitted because a complete runner policy requires this field.
With a built-in, omission inherits the constructor's restricted network policy.
An omitted or `null` proxy is omitted from the runner payload.
A supplied proxy object is forwarded as written; Console does not fill in its fields.
The pinned runner requires the seven scalar proxy fields shown in the example, including `enabled: true`.
Previously, Console filled in the other six scalar fields; configurations that relied on that behavior must now supply them explicitly.
`domains` and `unixSockets` may be omitted or `null`.
Incomplete proxy objects receive the native runner's validation error.
The [runner protocol](#configuration-fields-and-defaults) defines supported values and required fields.

The native proxy owns host normalization, pattern matching, local-network checks, and enforcement.
An empty domain allowlist grants no destinations.
When a proxy is supplied, the runner enforces managed proxy routing even with `network: enabled`; the native exceptions selected by `allowLocalBinding` still apply.

Console owns `version`, `lifecycle`, `extends`, and `workspace` inside the `sandbox` mapping and rejects attempts to set them there.
Select a built-in with top-level `extends`; Console captures `workspace` from the launch working directory.
These fields select its private launch protocol, profile and workspace identity, parent observation, signal handling, and worker temporary storage.
Use the [explicit complete-policy interface](#explicit-complete-policy) when selecting those fields for a standalone workload.
The native cleanup timeout is left omitted so the runner supplies its default.
Console also supplies its macOS extension for the restricted application policy unless `macos_seatbelt_profile_extension` is explicitly set, including to `null`.

Project `environment` and `inherit_environment` control ordinary workload variables.
For `serve`, Console preserves each worker generation's selected R/Python environment and dynamic-resolution setting after applying project controls, including when inheritance is disabled.
Project overrides cannot replace or reintroduce variables assigned or removed by that selection.
Host resolver configuration still comes from the server's launch environment.
Standalone `sandbox` launches apply native environment controls without these worker-generation overrides.
Both application launch paths retain `MCP_CONSOLE_SANDBOX=1` for runtime integration, including when inheritance is disabled or project environment entries try to replace it.
The explicit complete-policy interface leaves this marker under caller control.

Configuration must contain exactly one UTF-8 YAML 1.2 mapping document.
The `sandbox` mapping may be omitted; `{}` preserves defaults.
Console checks its top-level configuration fields, the `sandbox` mapping, and JSON-compatible YAML syntax.
Native policy validation and unknown-field handling belong to the runner; custom YAML tags are unsupported.
For duplicate keys, the last value wins because of a known limitation of Saphyr's node loader; this behavior is subject to change.
Booleans use YAML 1.2 values such as `true` and `false`; strings such as `yes` are not Boolean options.
Apart from the native built-in baseline and explicit adjustments, there are no includes, merge keys, interpolation, shell expansion, configuration layering, or reloads.
Parsing uses Saphyr's YAML node API in Rust and does not start Python or resolve a Python environment.
Scalar and tag resolution follow the pinned Saphyr loader.

The trusted outer process captures the workspace and reads and normalizes configuration once, before workload startup.
Worker directory changes do not move the workspace permission root.
When a project configuration file exists, it probes the native sandbox with a no-op process using those captured settings.
This checks native policy validation and sandbox/proxy startup before running the workload or announcing server readiness, without starting a worker.
Probe failures include the configuration filename and the runner's diagnostic; native JSON error locations refer to the generated runner policy.
`serve` retains that snapshot for the whole session, including when the configuration file did not exist at launch.
Editing, removing, or creating the file later cannot change the initial worker, a restart that resets its state, recovery after worker failure, or a replacement using newly prepared requirements.
Internal launches explicitly select the captured application settings through a child-specific environment payload and the public `sandbox` launch boundary; no child rereads a filename.
The sandbox layer adds application launch requirements and strips the private settings transport before launching the runner and workload.
Ambient `MCP_CONSOLE_SANDBOX_SETTINGS` or `MCP_CONSOLE_SANDBOX_CONFIG` values do not select policy, and the server's global environment is not modified.

`serve --no-sandbox` reads project configuration to retain target selection but does not enforce sandbox permission settings.
Malformed YAML and invalid target configuration are errors in this mode.
Explicit `sandbox --config-env NAME` also bypasses discovery and retains the complete-policy interface below.
Both still conflict with explicit `--writable-root` arguments.

### SSH target placement

Top-level `target` selects SSH execution for `serve` only; it does not select a permission policy.
For example:

```yaml
extends: ":workspace"
target:
  transport: {kind: ssh, host: mule}
  workspace: /srv/projects/analysis
  command: [uvx, mcp-console]
```

The server reads YAML locally and captures the target and user policy once.
It sends these settings as bounded structured data; the remote helper verifies the existing absolute workspace and materializes policy there without discovering remote YAML.
Platform defaults, relative filesystem entries, workspace special paths, and `serve --writable-root` use the remote host and workspace.
The native sandbox and any proxy run remotely.
`sandbox.environment` and `inherit_environment` retain their workload meaning, including with direct SSH execution; they do not forward the workload environment to SSH or trusted preparation.
Explicit remote `R_HOME` and `RETICULATE_PYTHON` values are conveyed separately as runtime selections so preparation targets the worker's runtime; the rest of the environment map remains workload-only.
Standalone `sandbox` still uses local paths and local policy materialization.
See [SSH execution](SSH.md) for every target field, defaults, error behavior, remote runtime and preparation prerequisites, and lifecycle limits.

## Explicit complete policy

Select a policy explicitly in the trusted process that launches the sandbox:

```sh
SANDBOX_POLICY='{"version":2,"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

`SANDBOX_POLICY` contains JSON, not a filename.
Console forwards the selected name to its verified private runner, which owns the configuration types, validation, and enforcement.
An explicit configuration supplies the complete runner request, including its optional native built-in selection; Console does not merge application defaults, temporary-directory exclusions, `.claude` protection, or its macOS extension into it.

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
These extra entries grant no access to their parents or home.
The selected built-in independently supplies its workspace baseline, including metadata defaults that a broader enclosing writable root does not erase.
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
Configured filesystem entries and CLI writable roots are additive; native validation applies to both.

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
Native Linux execution supplies `PWD` from the working directory if the target map omits it.
Native runtime additions, such as macOS locale metadata, follow the host runtime's behavior.

For native execution, target overrides take effect only after helper setup and enforcement.
They cannot select host helpers through `PATH`, relocate host setup through `TMPDIR`, inject code into host loaders, or configure host proxy/control state.
The trusted launch environment remains responsible for loading the frontend and runner and selecting host helpers.
Put target loader settings in the JSON `environment` map.
Use a dedicated transport name rather than a loader variable.

The selected name and reserved `MCP_CONSOLE_SANDBOX_CONFIG` are stripped from every helper and target environment, including explicit maps that try to reintroduce them.
Neither may also be a private-directory export.
`MCP_CONSOLE_SANDBOX` is an ordinary target marker, not an authorization check.

## Configuration fields and defaults

The pinned [runner protocol](https://github.com/t-kalinowski/codex/blob/2d0ad797210de821c07d1f18e4f1ffdcf06589cb/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) defines the complete canonical schema, including filesystem paths and precedence, proxy fields, and unknown-field handling.
Its filesystem and proxy fields use the upstream types and validators directly.

| Field                                       | Environment-mode contract                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `version`                                   | Required integer, currently `2`.                                                                                                                                                                                                                                                                                                                         |
| `filesystem`                                | Required without `extends`; omitted with a selector inherits its baseline. Supplied restricted entries augment that baseline. Object: `kind` is `"restricted"`, `"unrestricted"`, or `"external-sandbox"`. See [enforcement modes](#filesystem-and-enforcement-modes) and [filesystem classification](LINUX_COMPATIBILITY.md#filesystem-classification). |
| `network`                                   | Required without `extends`; omitted with a selector inherits restricted networking. Explicit values: `"restricted"` or `"enabled"`. Enabling networking does not grant filesystem writes. External execution without a proxy delegates network enforcement to the outer sandbox.                                                                         |
| `extends`, `workspace`, `workspace_options` | Optional native selection: `":workspace"` or `":read-only"`, a fixed absolute workspace (default command cwd), and the two temporary-directory switches for `":workspace"` (native defaults `false`). See the runner reference for composition and requiredness.                                                                                         |
| `proxy`                                     | Optional managed proxy configuration; omitted or `null` means none. A supplied configuration must be enabled. Its allowlist is selected by the trusted launcher.                                                                                                                                                                                         |
| `macos_seatbelt_profile_extension`          | Optional trusted SBPL appended to the native profile. It can grant permissions as well as restrict them. Omitted or `null` adds no extension; a supplied value is rejected on Linux or for external execution without a proxy.                                                                                                                           |
| `inherit_environment`                       | Optional Boolean, default `true`.                                                                                                                                                                                                                                                                                                                        |
| `environment`                               | Optional target override map, default empty. Keys must be nonempty and contain neither `=` nor NUL; values must be NUL-free.                                                                                                                                                                                                                             |
| `linux_backend`                             | Optional on Linux: `"bubblewrap"` (default when native execution is selected) or explicit `"landlock"`. Rejected on macOS.                                                                                                                                                                                                                               |
| `lifecycle`                                 | Optional object with the defaults below.                                                                                                                                                                                                                                                                                                                 |

| Lifecycle field      | Default and behavior                                                                                                                                                                                                                                                        |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parent_pid`         | Omitted/`null`: no caller-death observation. Otherwise it must identify the runner's actual current parent and be greater than 1.                                                                                                                                           |
| `sigterm`            | `"forward"`: forward SIGTERM and preserve the target's exit status. `"retire"`: retire the sandbox on SIGTERM.                                                                                                                                                              |
| `private_tmp`        | Omitted/`null`: create no private storage. Otherwise provide `environment`, an array of export names, and optionally an absolute `parent` directory. The default parent is the supervisor's native temporary directory. The runner creates and removes its private storage. |
| `cleanup_timeout_ms` | Omitted/`null`: `1000` for supervised execution; an explicit value must be between 1 and 60000. A retirement failure returns a nonzero status.                                                                                                                              |

These are the runner defaults for an explicit configuration.
Console's no-config path additionally requests its macOS extension and private `TMPDIR`.
For native managed execution, descendant retirement and the [supported lifetime boundaries](SANDBOX.md#supported-hosts-and-lifetime-limits) still apply.

Repeated `--config-env`, combining it with private `--exit-with-parent`, duplicate top-level JSON fields, and unknown top-level fields are rejected.
Use `lifecycle.parent_pid` in an explicit configuration.
The runner rejects simultaneous environment and descriptor input options.
The explicit interface has no configuration-file option, discovery, `@file` syntax, include, reload, or file fallback.

## Filesystem and enforcement modes

Project YAML and the complete-policy interface forward all filesystem kinds to the runner on macOS and Linux.
The pinned runner accepts the three kinds below.

| Filesystem kind    | Filesystem access                                 | Networking without a proxy                                    | Execution                                                                  |
| ------------------ | ------------------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `restricted`       | Native rules from `entries`                       | Native enforcement of the selected `network` value            | Native supervision                                                         |
| `unrestricted`     | Full filesystem access, subject to OS permissions | Native enforcement of the selected `network` value            | Native supervision, including Linux namespace init with enabled networking |
| `external-sandbox` | Enforcement delegated to an outer sandbox         | Both network values delegate enforcement to the outer sandbox | Ordinary process supervision; no native sandbox                            |

For example, this complete policy grants unrestricted filesystem access while requesting native network restrictions and private-directory cleanup:

```json
{
  "version": 2,
  "filesystem": { "kind": "unrestricted" },
  "network": "restricted",
  "lifecycle": { "private_tmp": { "environment": ["TMPDIR"] } }
}
```

Entries accompanying `unrestricted` or `external-sandbox` do not narrow access.
Unrestricted policies also discard the native writable-root metadata protections.
Full filesystem access can let a workload alter shared files or interfere with an unsandboxed process and thereby undermine network or supervisor restrictions.
The restricted-policy isolation guarantees do not extend to these modes; ordinary cleanup does not establish protection against a hostile unrestricted workload.

For any filesystem kind, a supplied enabled proxy selects native managed-network enforcement and routing, with either network value.
With `external-sandbox`, filesystem enforcement still belongs to the outer sandbox.

Without a proxy, `external-sandbox` neither creates an outer sandbox nor verifies that one exists.
In particular, `network: "restricted"` installs no network restriction in this mode.
The runner supplies environment, stdio, signal restoration and forwarding, configured caller-death handling, and optional private-storage cleanup.
Linux retires the original process group and waits for its direct child; the outer sandbox must retire descendants that leave that group.
macOS retains its existing descendant observation and process-group retirement, with the [documented observation limits](SANDBOX.md#supported-hosts-and-lifetime-limits).

## Explicit Linux backend selection

For managed policies, omitting `linux_backend` or selecting `"bubblewrap"` retains the default namespace sandbox and supervised lifetime, even when `lifecycle` is omitted.
Both support unrestricted filesystem policies and special root write grants without effective narrower rules, with either network setting.
Root write with effective read-only or denied carveouts proceeds to native validation; it is not equivalent to unrestricted access.
See the [filesystem classification](LINUX_COMPATIBILITY.md#filesystem-classification).
For `external-sandbox` without a proxy, explicit `"bubblewrap"` still delegates enforcement; it does not force a native sandbox.
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
The runner also rejects combining `external-sandbox` with the Landlock override.
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
