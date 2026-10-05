# Resolver sandbox

On macOS and Linux, local dependency preparation runs inside the native sandbox runner.
The existing preparation process, including discovery, installation, package builds, Python inspection, and DuckDB extension installation, uses one resolver policy.
SSH preparation retains execution-host permissions; its resolver sandbox is deferred.
Prepared Docker and Docker Sandbox sessions use preinstalled packages and do not run this resolver.
`serve --no-sandbox` uses ordinary host permissions.
Windows retains its host resolver and Job lifecycle; its native runner does not support managed proxy routing.

The default permits host-file reads, writes to the selected package caches and private temporary storage, and downloads through a managed proxy.
On macOS it also grants Darwin's user temporary directory, which `mktemp` and shell here-documents select independently of `TMPDIR`.
It allows PyPI, CRAN, Posit's package manager, Bioconductor, GitHub sources and Python releases, and DuckDB's extension repository.
Loopback binding is allowed for installer subprocess coordination.
On macOS this also permits access to host loopback services and DNS; Linux uses the runner's private network namespace.
These permissions are intended for ordinary package preparation, including source builds that use installed compilers and system libraries.
Resolver executables, wrappers, configuration, and package sources remain trusted inputs.
Cache placement does not make these inputs immutable or protect wrappers writable under the worker policy.

## Configuration

Use the top-level `resolver` mapping in `.agents/console/config.yaml`, the home configuration, or `-c` overrides.
It accepts native sandbox fields independently of the worker's `sandbox` mapping, profiles, and writable roots.
Console supplies `version`, `lifecycle`, and private temporary storage; `extends` and `workspace` are also reserved.
Other values pass through to the native runner for validation.
See [native policy fields](SANDBOX_CONFIGURATION.md).

Omitted filesystem kind, entries, network, and proxy fields receive resolver defaults.
Explicit `filesystem.entries` replaces all default entries; explicit `proxy.domains` replaces the download allowlist.
`proxy: null` disables the proxy.
An empty domains mapping allows no proxy destinations.
Literal paths are relative to the execution workspace; they do not expand `~` or environment variables.
Default cache directories are created before launch.
Create custom writable directories before launch on Linux, where absent roots cannot be bound.

Default cache grants cover the locations and direct environment overrides listed below.
Console does not inspect uv configuration files to discover additional writable paths.
If `UV_CONFIG_FILE` or `uv.toml` selects a custom `cache-dir`, set the matching `resolver.environment.UV_CACHE_DIR` or grant that path in `resolver.filesystem.entries`.
Use the expanded default as the starting point when supplying entries, since they replace all default grants.
Other storage settings and package sources may also need explicit filesystem or proxy permissions.

For a custom uv cache with an automatic write grant:

```yaml
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
```

For a different PyPI mirror:

```yaml
resolver:
  environment:
    UV_INDEX_URL: https://packages.example.org/simple
  proxy:
    domains:
      packages.example.org: allow
      files.pythonhosted.org: allow
      astral.sh: allow
      releases.astral.sh: allow
      github.com: allow
      release-assets.githubusercontent.com: allow
```

Include every host that serves the mirror's artifacts or redirects, plus the R and DuckDB sources needed by the session.
The proxy's native host matching and local-network checks apply.
Resolver environment values configure preparation; worker environment values do not.
R availability is discovered inside the resolver after its environment policy applies.
Resolver and worker policies preserve the server's Python selection: `python` in Console YAML, otherwise the server's `RETICULATE_PYTHON`.
Omit both to use managed Python.
Configuration and host cache paths are captured at Console startup and retained across preparation calls and worker restarts.
Custom workers can start without `HOME` when they do not request host preparation; launching a sandboxed resolver still requires an absolute `HOME`.
Changes to a running worker's environment do not reconfigure the resolver.
The `resolver` mapping applies only to local sandboxed preparation.

For a shared DuckDB cache at a different path:

```yaml
resolver:
  environment:
    MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY: /home/alice/package-caches/duckdb
```

Console captures this path for installation and the managed R/Python SQL connections.
With the default filesystem policy, an omitted path uses `extensions` beneath the cache namespace below.
An explicit filesystem policy retains `.duckdb/extensions` beneath the resolver's effective `HOME`, even when it differs from the server or worker `HOME`.
Custom workers receive the same captured path at launch, before accepting their first managed R layer.
User-selected Python retains DuckDB's own cache settings and preinstalled extensions.
An explicit `filesystem.entries` must grant writes to the selected directory.

## Default cache namespace

With the default local resolver filesystem policy, omitted cache locations use `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/resolver/payload`.
An empty or relative `XDG_CACHE_HOME` uses the HOME fallback.
uv caches, Python installations and tools, IR, R, renv, pak, Matplotlib, and DuckDB extensions have separate directories beneath this namespace.
The existing native runner supplies filesystem enforcement, the download proxy, private temporary storage, and process retirement.
Workers link the warmed font cache into their private Matplotlib directory.
Host Matplotlib configuration remains selected independently of this cache.

Explicit cache environment values remain effective, including inherited values.
Empty cache values use the Console defaults.
An explicit `resolver.filesystem.entries` retains the existing host cache layout and requires its matching write grants.
`--no-sandbox`, Windows preparation, and SSH preparation retain host cache selection.
No broker protocol, cache lease, or automatic cache deletion is added.

## Expanded policy for shared host caches

For a Linux account with `HOME=/home/alice`, the following explicit filesystem policy selects shared host caches.
The `macos_seatbelt_profile_extension` field is absent on Linux.
On macOS Console supplies the same [trusted application extension](../src/sandbox/policy_extensions.sbpl) as the worker unless explicitly overridden, including by null.
Version, caller observation, cleanup, and private `TMPDIR` are supplied by Console and are omitted here.

```yaml
resolver:
  filesystem:
    kind: restricted
    entries:
      - path: {type: special, value: {kind: root}}
        access: read
      - path: {type: path, path: /home/alice/.cache/uv}
        access: write
      - path: {type: path, path: /home/alice/.local/share/uv/python}
        access: write
      - path: {type: path, path: /home/alice/.local/share/uv/tools}
        access: write
      - path: {type: path, path: /home/alice/.cache/R/ir}
        access: write
      - path: {type: path, path: /home/alice/.cache/R/reticulate}
        access: write
      - path: {type: path, path: /home/alice/.duckdb/extensions}
        access: write
      - path: {type: path, path: /home/alice/.cache/matplotlib}
        access: write
      - path: {type: path, path: /home/alice/.cache/R/renv}
        access: write
      - path: {type: path, path: /home/alice/.cache/R/pkgcache}
        access: write
  network: restricted
  proxy:
    enabled: true
    enableSocks5: true
    enableSocks5Udp: false
    allowUpstreamProxy: false
    dangerouslyAllowAllUnixSockets: false
    allowLocalBinding: true
    mode: full
    domains:
      pypi.org: allow
      files.pythonhosted.org: allow
      astral.sh: allow
      releases.astral.sh: allow
      github.com: allow
      api.github.com: allow
      codeload.github.com: allow
      raw.githubusercontent.com: allow
      objects.githubusercontent.com: allow
      release-assets.githubusercontent.com: allow
      r-lib.github.io: allow
      packagemanager.posit.co: allow
      rspm-sync.rstudio.com: allow
      bioconductor.posit.co: allow
      bioconductor.org: allow
      cran.r-project.org: allow
      cloud.r-project.org: allow
      cran.rstudio.com: allow
      extensions.duckdb.org: allow
  inherit_environment: true
  environment: {}
```

With an explicit filesystem policy, cache paths follow the resolver's effective environment:

| Cache               | Selection                                                                                                                                                |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| uv cache            | `UV_CACHE_DIR`, otherwise `${XDG_CACHE_HOME:-$HOME/.cache}/uv`                                                                                           |
| uv Python and tools | `UV_PYTHON_INSTALL_DIR` / `UV_TOOL_DIR`, otherwise `${XDG_DATA_HOME:-$HOME/.local/share}/uv/python` / `uv/tools`                                         |
| IR                  | `IR_CACHE_DIR`, otherwise R's cache base plus `R/ir`                                                                                                     |
| reticulate          | R's cache base plus `R/reticulate`                                                                                                                       |
| renv                | `RENV_PATHS_ROOT`, otherwise R's cache base plus `R/renv`; explicit `RENV_PATHS_CACHE`, `RENV_PATHS_SOURCE`, and `RENV_PATHS_BINARY` also receive writes |
| pak/pkgcache        | R's cache base plus `R/pkgcache`; explicit `PKG_CACHE_DIR` and `R_PKG_CACHE_DIR` also receive writes                                                     |
| DuckDB              | `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY`, otherwise `$HOME/.duckdb/extensions`                                                                           |
| Matplotlib          | `MPLCONFIGDIR`, otherwise `${XDG_CACHE_HOME:-$HOME/.cache}/matplotlib`                                                                                   |

R's cache base is `R_USER_CACHE_DIR`, then `XDG_CACHE_HOME`, then `$HOME/Library/Caches/org.R-project.R` on macOS or `$HOME/.cache` on Linux.
An explicit `IR_LIBRARY_ROOT` also receives writes.
macOS additionally permits uv's legacy `$HOME/Library/Caches/uv` and `$HOME/Library/Application Support/uv` locations.
Existing uv and reticulate cache selection remains unchanged.
For uv grants, relative `XDG_CACHE_HOME` and `XDG_DATA_HOME` values are ignored in favor of the defaults beneath `HOME`.
An absolute `HOME` is needed for resolver sandbox setup.
Selected cache paths must be UTF-8, as required by the native policy protocol.
Host cache selection lives in [`src/resolver/cache.rs`](../src/resolver/cache.rs), shared by resolver grants and captured DuckDB runtime paths.

## Trust boundary

Package code can read host files and modify the granted caches.
Cache artifacts remain shared with workers and other host processes; this change does not make those artifacts immutable or validate package code.
A custom policy can widen these permissions.
Use trusted requirements, resolvers, configuration, and package sources.
The [native runner's lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits) also apply to the resolver.
