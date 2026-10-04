# Resolver process boundary

On macOS and Linux, dependency preparation and runtime inspection use a separate process tree:

```text
server → hidden mcp-console resolve broker
           → verified native sandbox runner
               → resolver-workload → uv / ir / R / Python / build subprocesses
```

The server retains requirements, worker generations, candidates, and activation transactions.
`src/resolver/preparation.rs` is its preparation interface.
The broker owns trusted policy, storage, leases, ordinary child supervision, and data validation.
It launches the current Console executable directly.
It does not import packages, load returned native libraries, or execute returned interpreters.

The complete executable preparation graph runs in the resolver workload: runtime discovery, version probes, ir/reticulate bootstrap, Python selection and embedding inspection, Matplotlib warming, and DuckDB extension preparation.
R-present and Python-only sessions use the same implementation.
On SSH, the preparation transport starts this broker on the execution host.
Docker and Docker Sandbox continue to use prepared images with dynamic preparation disabled.
Windows retains host preparation with Job-owned descendants; the native resolver policy and owned payload described here are not available there.
See [Windows support](WINDOWS.md).

## Trusted launch data and private protocol

The server captures resolver configuration before accepting worker requests.
Trusted launch data and operation requirements occupy separate protocol fields.
Requirements cannot choose a command, filesystem grant, proxy policy, or cache root.
Python registry syntax, automatic R package names, explicit ir reference framing, and DuckDB extension names retain their public validation.

The version 6 preparation protocol uses JSON Lines locally and length-prefixed JSON frames over SSH, bounded to 1 MiB including assembled results.
The workload receives bounded versioned JSON through stdin and returns JSON through stdout.
Installer logs and native diagnostics use separate captured streams.
Their bounded text previews retain the beginning and end with UTF-8 omission counts; a large installer log does not invalidate the protocol result.
No submitted cells or interactive stdin enter preparation requests.

The broker and native runner start with an empty environment.
Captured resolver settings are transmitted as data and applied to the workload after native enforcement, including loader variables and interpreter startup settings.
Clearing a variable after launching an interpreter would not provide this boundary.
Python isolated mode remains an inspection behavior, not enforcement.

Results include the accepted manifest, interpreter identity and embedding configuration, R library paths, and extension storage.
The broker validates managed paths as data beneath its payload root and checks manifest identity.
Virtualenv executable spelling is retained.
Result files used by interpreter helpers retain their original open descriptor; the broker never opens a sandbox-controlled result pathname.
Sandbox output is untrusted even after a successful operation, and no cache scan or content approval is implied.

## Native policy and storage

Sandboxed sessions use exactly this owned namespace:

```text
${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/resolver/
    control/     broker-only locks and cleanup metadata
    payload/     uv, managed Python, ir, R caches, extensions and scratch
```

The resolver has an explicit native read allowlist for system libraries, selected runtime installations, executable tools, and certificates.
It also exposes Linux distribution metadata used by pak and renv to select compatible binary packages.
On macOS, it captures `DEVELOPER_DIR` or the stored system developer-directory selection and grants read access to that toolchain, including the selected Xcode bundle and its system license receipt.
Apple tool shims may report denied attempts to use their host lookup cache; those attempts do not receive extra filesystem permissions.
It does not receive the worker's host-readable profile, workspace grants, home credentials, or host package caches.
Its HOME and XDG directories, uv caches and installations, ir libraries, R/renv caches, Matplotlib cache, and native private temporary directory are beneath payload.
Ordinary host caches are neither seeded nor mounted for reuse.
Supported installers must respect this filesystem boundary; a cache environment variable alone is not enforcement.
Managed uv bootstrap downloads and unpacks the platform release archive inside the workload.
The upstream shell installer uses macOS temporary-file APIs that ignore `TMPDIR`, so it cannot run with the resolver's private temporary storage.

Console and its native runner must be installed outside this namespace, so packages cannot replace launch components and cleanup cannot delete them.
An explicitly selected Python executable grants its own file and standard non-symlink library directories.
A copied virtualenv whose base installation is elsewhere needs that base in trusted `resolver.readable_roots`; `pyvenv.cfg` does not grant host permissions.

Package downloads use the native managed proxy.
Its default-deny host policy allows the built-in Python, R, bootstrap, and DuckDB package sources and their download hosts.
Trusted `resolver.allowed_hosts` entries add native host patterns.
`resolver.environment` supplies trusted workload settings, such as a private index, and `resolver.readable_roots` supplies extra runtime or certificate paths.
None supplies write grants or an unrestricted-network fallback.

```yaml
resolver:
  allowed_hosts: [packages.example.org, downloads.example.org]
  environment:
    UV_INDEX_URL: https://packages.example.org/simple
  readable_roots: [/opt/company/ca.pem]
```

An allowed destination can receive anything readable by a package.
Read-only access plus networking is not a confidentiality boundary.
Choose additional readable roots and destinations accordingly.
Downloads may redirect to different hosts; a cold installation verifies permissions that a warm cache can hide.

The resolver permits loopback sockets for installer subprocess communication.
renv chooses an ephemeral port internally and exposes no configuration setting for a caller-selected port.
Linux confines these sockets to its private network namespace; on macOS, the native allowance also permits connections to host loopback services and outbound DNS on port 53.
External package destinations remain restricted by the managed proxy.
Missing namespace, proxy, or filesystem enforcement fails closed.

Workers receive accepted artifacts read-only.
The broker also supplies its warmed Matplotlib cache to workers; each worker links those files into its private configuration directory while preserving the selected host `matplotlibrc`.
Console adds native read rules for the resolver namespace and installed Console bundle.
This implementation accepts the default, `:workspace`, and `:read-only` worker filesystem profiles without custom filesystem rules or native extensions.
Other combinations are rejected because those extra rules could override the protections.
Worker workspaces cannot be inside protected storage.
Native code owns path and precedence semantics; the resolver does not implement a second permission matcher.

`--no-sandbox` explicitly disables this enforcement.
Preparation then uses the captured host environment and ordinary host caches.
It does not automatically consume the sandboxed resolver namespace.
Package code has the execution account's permissions in that mode.

## Lifetimes and weekly cleanup

Each broker holds a lease while its preparation connection is open.
Idle periods, retained environments, and gaps between restarts remain leased.
A protected gate lock serializes lease metadata and cleanup.
Each resolver and worker native launcher also retains an independent shared lock until native retirement; its descriptor is not inherited by package or worker code.
The kernel locks exclude deletion even when a broker dies while a worker retains an environment.

On resolver use, the broker checks the last successful cleanup timestamp.
After seven days, it removes and recreates the whole payload only when no lease remains.
Removal does not follow sandbox-created symlinks.
The timestamp changes only after successful recreation.
There is no daemon, scheduled task, cache seeding, per-entry aging, or periodic scan.

After owner loss, the last native launcher's exit releases its lock, allowing a later use to perform overdue cleanup.
The broker count in metadata is diagnostic and is reset after acquiring an exclusive lock.
An explicitly unconfirmed retirement marks storage uncertain and prevents automatic deletion; independent recovery after native runner failure is unsupported.

`ResolverStopHandle` routes interruption, cancellation, and input closure to the current operation.
The native runner owns descendant retirement.
Interrupt acknowledgment confirms signal delivery; preparation remains pending until native retirement completes.
The broker accepts a result only after successful native launcher exit; workload JSON is never evidence of cleanup.
Forced termination and diagnostic collection remain bounded when a runner cannot retire.
A forced exit never confirms descendant retirement.
Unconfirmed retirement blocks further preparation and replacement.
For explicit direct execution, existing process-group cleanup limits still apply.

The server resolves and inspects the entire candidate before retiring an existing worker.
Preparation failure retains the accepted manifest, objects, and queued input and does not dispatch same-call stdin or code.
Live R/Python activation retains its existing worker-confirmed commit boundary.
Python-only managed sessions also support compatible live additions under the [live preparation contract](REQUIREMENTS.md#live-python-preparation), and record only accepted Python declarations in the Quarto package list.
