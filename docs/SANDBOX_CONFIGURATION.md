# Sandbox configuration

To edit the workspace and call one API:

```yaml
sandbox:
  filesystem:
    read_write: [.]
  network:
    proxy:
      domains:
        allow: [api.example.com]
```

Configuration is trusted and can widen permissions.
[Configuration](CONFIGURATION.md) defines discovery and ordered CLI layering.
This schema also applies unchanged to `resolver.sandbox`; neither policy inherits the other's permissions.

## Filesystem

```yaml
sandbox:
  filesystem:
    read_only: [./data]
    read_write: [.]
    deny: [./secrets]
```

Groups compile to native concrete-path entries with `read`, `write`, and `deny` access, respectively.
There are no glob entries, native kinds, profiles, or expansion operations in the public format.
Omitted groups in an explicit mapping are empty.
Worker entries modify its existing host-read/private-write baseline; omission grants no workspace writes.
An omitted resolver filesystem retains its cache defaults; an explicit mapping replaces its default entries, including host reads and cache grants.
`resolver.sandbox.filesystem: {}` therefore supplies no configured grants.
Include necessary reads and writable caches explicitly; see [resolver policy](RESOLVER.md).

Native specificity controls access: narrower paths take precedence; at equal paths deny wins over write, then read.
Group and list order do not define precedence.
The native runner protects writes to `.git`, `.agents`, `.codex`, and `.aws` beneath writable roots; explicit equal-path or narrower write grants can override those protections.
Broader writable roots can subsume narrower roots and their native metadata defaults.
Include fixed workspace metadata paths in `read_only` when granting an ancestor directory to retain those guards.
It does not add metadata read grants beneath denied ancestors.
To retain the former workspace profile's additional `.claude` protection, configure `read_only: [.claude]` explicitly.
The trusted Console macOS extension remains Console-owned.

Configured paths and `--writable-root` grants resolve against the fixed launch workspace.
They do not expand `~` or variables, create directories, grant parents, or erase symlink components.
Private temporary storage and cache adjustments retain their existing ownership.
macOS can grant individual files and permit creation at absent granted paths.
Linux skips absent write roots until another launch and can reject existing file roots during metadata preparation.
Nested read exceptions beneath denials can remain hidden by Linux mount masking, or deeper denials can fail setup.
Console neither rewrites these policies nor switches backends; a successful parser test does not prove those native grants work.

## Network choices

Choose one of:

```yaml
sandbox:
  network: restricted
```

```yaml
sandbox:
  network: enabled
```

```yaml
sandbox:
  network:
    proxy: {}
```

Scalars select native networking without a managed proxy.
`enabled` changes networking only; filesystem enforcement stays in place.
A mapping requires a non-null `proxy` mapping and compiles to restricted native networking with an enabled managed proxy.
There is no public proxy-enabled Boolean or `network: full` shortcut.
Explicit scalars also remove the resolver's generated download proxy.

Omitted worker networking is restricted without a proxy.
Omitted resolver networking preserves its download policy on macOS/Linux; Windows preparation retains host networking and permissions.
For managed mappings, mode defaults to full, full-mode SOCKS to TCP, upstream routing to false, and Unix sockets to an empty allowlist.
Local binding defaults to false for workers and true for resolvers.
Omitted domains use the process's destination default: empty for workers and the download allowlist for resolvers.
An explicit `domains` mapping replaces that generated default; omitted allow/deny lists are empty.
`domains: {allow: []}` clears the resolver allowlist.
This replacement does not change the generic file/CLI map merge algorithm.

## Proxy modes and destinations

```yaml
sandbox:
  network:
    proxy:
      mode: limited
      domains:
        allow: [api.example.com, "*.example.org"]
        deny: [blocked.example.org]
```

Full mode accepts `domains`, `allow_upstream_proxy`, and `socks5`.
Limited mode accepts `domains` and `allow_upstream_proxy`; every explicitly supplied `socks5` selector is rejected, even `disabled`.
Generated defaults are selected after the mode, so limited mode always sends both native SOCKS flags as false.
A CLI mode change that leaves an explicit SOCKS selector present is an error; clear and rebuild the proxy mapping to remove it.

| Full-mode `socks5` | Native `enableSocks5` | Native `enableSocks5Udp` |
| ------------------ | --------------------- | ------------------------ |
| `disabled`         | false                 | false                    |
| `tcp` (default)    | true                  | false                    |
| `tcp_udp`          | true                  | true                     |

The pinned limited proxy permits plain HTTP GET, HEAD, and OPTIONS and rejects POST, HTTPS CONNECT, and SOCKS traffic.
It has no TLS interception and provides no read-only HTTPS or side-effect guarantee.

Domain lists compile to the native domain-permission map.
Matching and normalization remain native: an exact hostname covers only that host; `*.example.org` covers subdomains at any depth but not the apex; `**.example.org` covers both.
Matching deny rules take precedence over allows.
Host-with-port and URL-path inputs are rejected because native matching would discard those restrictions.
IPv4, bracketed/unbracketed IPv6, and scoped IP literals remain supported.

`allow_upstream_proxy` maps to `allowUpstreamProxy` and defaults to false.
It selects onward routing using the trusted launch environment, without overriding destination or method policy.
Workload environment overrides cannot choose that trusted hop.
There are no port rules, URL rules, credential injection, listener addresses, TLS interception, or per-request approvals in this format.

## Unix sockets and local binding

```yaml
sandbox:
  network:
    proxy: {mode: limited}
    sockets:
      unix_sockets: [/tmp/service.sock]
    allow_local_binding: true
```

`unix_sockets` accepts only a literal absolute-path list or `dangerously_allow_all`.
A list, including `[]`, generates a native `unixSockets` allow map and clears `dangerouslyAllowAllUnixSockets`.
Allow-all generates an empty map and sets that Boolean to true.
There are no deny lists, Boolean shortcuts, globs, or aliases.
Native macOS direct socket grants use subpath matching; HTTP-to-socket forwarding has its own native matching.
The allowlist is not an exact-match firewall across all native socket paths, and filesystem permissions still apply.

`allow_local_binding` is alongside `proxy` and `sockets`, and maps to native `proxy.allowLocalBinding`.
It affects private-address destination checks and native local socket behavior, including macOS loopback traffic and DNS.
It does not expose a Linux network namespace to a host browser.
Both socket controls ultimately populate the native proxy object; this organization adds no proxy-free enforcement path.

Limited mode leaves explicit socket and local permissions intact.
Direct connections granted by those permissions do not pass through the proxy's method filter.
Limited mode with those grants therefore does not restrict every channel to HTTP GET/HEAD/OPTIONS.

## Native capabilities

One schema and normalization path serves all platforms; native capabilities still differ.
Explicit managed proxy mappings are rejected on Windows, where the pinned runner has no managed proxy.
Explicit `resolver.sandbox` requests are rejected on Windows because dependency preparation remains unsandboxed.
Environment and cache controls still apply there.

Linux's managed bridge carries TCP only; explicit `tcp_udp` and nonempty Unix-socket path lists are rejected before launch.
Unix-socket allow-all is supported by the pinned Linux filter; it does not provide path-specific grants.
An empty socket list remains valid.
The pinned native SOCKS UDP reply path sends received replies back to the destination rather than the client, including on macOS.
`tcp_udp` is therefore rejected on this pin on macOS as well, even with local binding enabled.
The table above describes the native flag mapping; it does not claim usable UDP routing.
A local native fixture records the reply returning to the origin; closing this runner limitation is outside this format change.
Successful macOS normalization and HTTP tests establish no Linux/Windows enforcement parity.
See [sandbox limits](SANDBOX.md) and [Windows](WINDOWS.md).

## Explicit complete policy

A trusted standalone caller can bypass discovery and all Console defaults:

```sh
SANDBOX_POLICY='{"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

The value is JSON, not a path.
No application extension, `.claude` protection, private TMPDIR, or profile adjustment is merged.
The runner consumes the chosen variable once and strips it and reserved transport names from helper/target maps, including attempted reintroduction.
`--config-env` rejects `-c`, writable roots, and conflicting private handoffs.
Stdin always belongs to the target.

The pinned [runner protocol](https://github.com/t-kalinowski/cobox/blob/85d407d8a4544ed0916aff3c7a273461e739f215/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) is the canonical complete schema.
The private protocol is unversioned; a `version` field is rejected.
Key differences from Console's application policy: filesystem/network are required without a profile; environment inheritance defaults true; lifecycle storage and caller observation are opt-in.
On Unix, `parent_pid` must identify the actual caller; supervised cleanup defaults to 1000 ms, with explicit values from 1 to 60000 ms.
Windows observes the runner's direct parent plus an optional session owner and uses a fixed five-second Job retirement deadline.

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

Windows application launches use the native `elevated` default and require explicit `mcp-console sandbox-setup` provisioning.
Native `windows_state_dir` can select an absolute persistent state directory.
An explicitly selected `unelevated` backend requires `network: enabled` and host reads; it rejects read-deny policy.
Neither mode silently weakens policy when a feature is unavailable.
Managed proxy configuration and custom cleanup timeouts are currently unsupported.
See [Windows support](WINDOWS.md#native-sandbox) for lifecycle and setup details.

## Explicit Linux backend selection

Omitted/`bubblewrap` uses namespace supervision for native execution, including full-write policies.
Namespace failure never causes a backend switch or unsandboxed retry.
The inherited-procfs alternative retains the same backend and policy.
External mode without a proxy still delegates enforcement with `bubblewrap` set.

Standalone `linux_backend: landlock` has been removed and is rejected before native setup.
Omit `linux_backend` or select `bubblewrap` explicitly.
See [Linux compatibility](LINUX_COMPATIBILITY.md#policy-and-backend-contract).

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
