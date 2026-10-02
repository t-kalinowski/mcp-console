# Sandbox configuration

Launcher configuration is trusted and can widen permissions.
Review it before starting Console.
[Configuration layering](CONFIGURATION.md) owns YAML discovery, `-c` overrides, and merge rules; this page owns policy composition and enforcement.

## Project configuration

`sandbox.provider: native` is the default for local, SSH, and ordinary Docker.
Docker Sandbox selects `compute`, which accepts only `provider`, `environment`, and `inherit_environment` under `sandbox`; native fields, top-level `extends`, and CLI writable roots are invalid.
See [SBX](DOCKER_SANDBOX.md).

For project editing:

```yaml
extends: :workspace
```

`:workspace` grants workspace writes while `.git`, `.agents`, `.codex`, and `.claude` are readable but protected from writes by default.
`:read-only` selects the native read-only baseline.
Omission keeps Console's host-read/private-write policy.
These reserved names include the colon; there are no user-defined profile chains.
One-off selection works with `-c extends=:workspace`.

The runner's constructors own Git pointer, symlink, and missing-path handling.
Console adds `.claude` and, for the workspace profile, defaults `workspace_options.exclude_tmpdir_env_var` and `exclude_slash_tmp` to `true`.
Thus shared `/tmp` and inherited `TMPDIR` are not writable merely because they exist; runner-private storage stays writable.
Explicit option values pass through; an explicit null options object restores native defaults, including those grants.

Restricted entries augment the baseline; an empty array does not erase it.
Explicit networking replaces its network setting.
Unrestricted/external filesystem kinds replace the baseline filesystem.
Native specificity applies: narrower paths beat ancestors; at equal paths, deny beats write, which beats read.
Metadata protections are **defaults, not mandatory ceilings**.
For example:

```yaml
extends: :workspace
sandbox:
  filesystem:
    entries:
      - path: {type: path, path: .claude}
        access: write
```

A read entry grants reads as well as limiting broader writes.
macOS can reopen reads beneath a broader denial; Linux mount masking may hide that grant or fail on a deeper nested denial.
Console does not rewrite policy or switch backends to hide native limitations.

Console owns `version`, `lifecycle`, `extends`, and `workspace` inside the sandbox mapping.
Select profiles at the top level; workspace is captured from launch.
Other native fields/types pass through for runner validation, not a parallel Console allowlist.
An omitted macOS extension gets Console's trusted extension for restricted application policy; explicit values, including null, override it.

## Additional writable paths

```sh
mcp-console serve --writable-root './output files' --writable-root /absolute/cache
```

Configured literal paths and CLI roots resolve against the fixed execution workspace, not the YAML directory.
They do not expand `~` or variables, grant parents, create directories, or erase symlink components.
Values must be UTF-8.
The captured paths survive worker restarts; they are persistent user data and are never deleted with private storage.

macOS permits individual files and workload creation of absent granted paths.
The pinned Linux backend skips absent paths until a later launch and fails for existing individual file roots during metadata preparation.
It does not substitute a parent-directory grant.
Metadata defaults also survive a broader enclosing writable-root grant unless native specificity deliberately overrides them.

`--writable-root` conflicts with `serve --no-sandbox` and explicit `sandbox --config-env`; the latter supplies a complete policy, not a merge.

## Networking and proxy

Network permission does not imply filesystem permission.
Without a selected profile, Console fills an omitted network setting with `restricted`; profiles inherit their own restricted default.
An omitted/null proxy means no proxy.
A supplied proxy is forwarded without filling required scalar fields:

```yaml
sandbox:
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

The runner owns host normalization, matching, local-network checks, and routing.
An empty allowlist allows no destinations.
A supplied enabled proxy enforces managed routing even with `network: enabled`, subject to explicit local-binding exceptions.
Proxy endpoints are execution-host addresses, including SSH/Docker.

## Command and environment

Use `--` before the standalone command.
Arguments are passed separately without an inserted shell; option-looking values after the command are target arguments.
Cwd is selected by the caller's process API, not a configuration `command`/`cwd`.

`environment` supplies workload overrides; `inherit_environment: false` makes that the complete ordinary workload map.
In native execution these values apply after helper setup, so they cannot select host helpers, move setup storage, or inject host-loader code.
The trusted launch environment still controls frontend loading and helper selection.

For `serve`, Console reapplies the selected R/Python generation environment and resolution policy after workload controls, even without inheritance.
Workload settings do not configure trusted resolvers; supported remote runtime selections are conveyed separately.
Ordinary application launches preserve `MCP_CONSOLE_SANDBOX=1`; the marker is not authorization.

Settings are captured once and retained across initial launch, restart, and failure recovery.
When selected configuration requires it, a no-op native preflight validates policy/setup before workload launch without starting an analysis worker.
Children receive captured values, never a filename to rediscover.
Ambient private transport variables cannot select policy.

For targets, see [SSH](SSH.md), [Docker](DOCKER.md), and [SBX](DOCKER_SANDBOX.md).
`--no-sandbox` still parses target configuration and preserves the outer provider; it is not a way to bypass malformed configuration or unsupported SBX fields.
Standalone `sandbox` remains local and rejects compute-provider enforcement.

## Explicit complete policy

A trusted standalone caller can bypass discovery and all Console defaults:

```sh
SANDBOX_POLICY='{"version":2,"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

The value is JSON, not a path.
No application extension, `.claude` protection, private TMPDIR, or profile adjustment is merged.
The runner consumes the chosen variable once and strips it and reserved transport names from helper/target maps, including attempted reintroduction.
`--config-env` rejects `-c`, writable roots, and conflicting private handoffs.
Stdin always belongs to the target.

The pinned [runner protocol](https://github.com/t-kalinowski/cobox/blob/6a18b21c2e75a10229a842424403d71cbd1e60ef/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) is the canonical complete schema.
Key differences from Console's application policy: `version: 2` is required; filesystem/network are required without a profile; environment inheritance defaults true; lifecycle storage and caller observation are opt-in.
On Unix, `parent_pid` must identify the actual caller; supervised cleanup defaults to 1000 ms, with explicit values from 1 to 60000 ms. Windows observes the runner's direct parent plus an optional session owner and uses a fixed five-second Job retirement deadline.

Use the runnable [shell](../examples/sandbox-config.sh), [Python](../examples/sandbox-config.py), or [R](../examples/sandbox-config.R) examples.
They pass child-specific environment maps rather than mutating a multithreaded parent's global environment.

## Filesystem and enforcement modes

| Filesystem kind    | Meaning without a proxy                                                                                    |
| ------------------ | ---------------------------------------------------------------------------------------------------------- |
| `restricted`       | Native filesystem rules and selected network policy, with native supervision.                              |
| `unrestricted`     | Full OS-permitted filesystem access; native supervision/network policy remain. Entries do not narrow it.   |
| `external-sandbox` | Delegate filesystem **and networking** to an outer sandbox. It neither creates nor verifies that boundary. |

In external mode without a proxy, `network: restricted` installs no network restriction.
Linux retires the original group/direct child; the outer sandbox must retire escaped descendants.
macOS retains its documented observation limits.
A supplied enabled proxy selects native managed networking for any kind, while external filesystem enforcement remains delegated.

Full filesystem writes can modify shared files or influence unsandboxed processes, undermining network/supervisor restrictions.
Restricted-policy isolation guarantees do not extend to hostile unrestricted workloads.
Cleanup is not a proof of isolation.
See [lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits).

## Windows backend selection

Windows defaults to `sandbox.windows_sandbox_level: elevated` and requires explicit
`mcp-console sandbox-setup` provisioning. `sandbox.windows_state_dir` can select an
absolute persistent state directory. An explicitly selected `restricted-token`
backend requires `network: enabled` and host reads; it rejects read-deny policy.
Neither mode silently weakens policy when a feature is unavailable. Managed proxy
configuration and custom cleanup timeouts are currently unsupported. See
[Windows support](WINDOWS.md#native-sandbox) for lifecycle and setup details.

## Explicit Linux backend selection

Omitted/`bubblewrap` uses namespace supervision for native execution, including full-write policies.
Namespace failure never causes a backend switch or unsandboxed retry.
The inherited-procfs alternative retains the same backend and policy.
External mode without a proxy still delegates enforcement with `bubblewrap` set.

Standalone `linux_backend: landlock` selects direct Landlock/seccomp execution: no namespaces, supervisor, descendant cleanup, or private storage.
It rejects proxy routing, external mode, caller observation, retirement SIGTERM, and explicit cleanup/storage options.
Console's server does not select it.

Landlock supports only policies its native representation can preserve, not restricted reads or unsupported carveouts.
Restricted filesystem enforcement requires ABI 3 truncate support; explicit full-write policies skip that filesystem requirement but still apply selected network policy.
Device ioctl restrictions are not a portable guarantee, and same-user host signaling may remain possible.
See [Linux compatibility](LINUX_COMPATIBILITY.md#filesystem-classification).

## Integrity, stdin, and large requests

The OS copies the launch environment; later parent or target changes cannot alter accepted policy.
Removing transport variables prevents propagation, not caller authentication or secure memory erasure.
The trusted launcher owns initial loader state, executable choice, policy, and authority conveyed by stdio.

JSON is limited to 1 MiB, but native exec limits can be smaller and include argv, environment, and platform overhead.
An initial oversized exec may fail with `E2BIG` before Console can print anything.
Malformed-policy diagnostics do not echo complete values.
Native protocol 2 requires UTF-8 inputs.

On Unix the private runner also supports `--bootstrap-fd N`: an inherited readable fd above stderr contains a four-byte big-endian length and 1–1,048,576 JSON bytes.
That form includes command, absolute cwd, and a complete target environment and rejects inheritance.
Start the reader before writing more than a pipe buffer.
The trusted writer must control all bytes until complete acceptance; the runner then closes the descriptor without waiting for EOF.
Neither transport spills policy to mutable files or bypasses the final target's exec limits.
