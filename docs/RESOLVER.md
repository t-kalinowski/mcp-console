# Resolver sandbox

On macOS and Linux, local dependency preparation runs inside the native sandbox runner.
The existing preparation process, including discovery, installation, package builds, Python inspection, and DuckDB extension installation, uses one resolver policy.
`serve --no-sandbox` uses ordinary host permissions.
Windows retains its host resolver and Job lifecycle; its native runner does not support managed proxy routing.

The default permits host-file reads, writes to Console's package cache and private temporary storage, and downloads through a managed proxy.
It allows PyPI, CRAN, Posit's package manager, Bioconductor, GitHub sources and Python releases, and DuckDB's extension repository.
Loopback binding is allowed for installer subprocess coordination.
On macOS this also permits access to host loopback services and DNS; Linux uses the runner's private network namespace.
These permissions are intended for ordinary package preparation, including source builds that use installed compilers and system libraries.
Resolver executables, wrappers, configuration, and package sources remain trusted inputs.
Cache placement does not make these inputs immutable or protect wrappers writable under the worker policy.

## Cache locations

Local sandboxed sessions default to `cache: console`.
Downloaded Python installations, uv environments, IR libraries, reticulate tooling, renv packages, pak metadata, and DuckDB extensions live beneath one Console cache directory:

| Platform | Default directory                               |
| -------- | ----------------------------------------------- |
| Linux    | `$HOME/.cache/mcp-console/dependencies`         |
| macOS    | `$HOME/Library/Caches/mcp-console/dependencies` |
| Windows  | `%LOCALAPPDATA%/mcp-console/cache/dependencies` |

An absolute `XDG_CACHE_HOME` selects `<XDG_CACHE_HOME>/mcp-console/dependencies` on any platform.
This selection does not require `HOME`.
The root uses the resolver's effective environment, before Console redirects the cache variables.
On Windows, missing `LOCALAPPDATA` uses `USERPROFILE/AppData/Local` or the absolute `HOME` equivalent.
Console captures this selection once for preparation and worker restarts.
The writable dependency directory is separate from `mcp-console/sandbox`, which holds the source checkout and build artifacts used for host-side companion staging.
On macOS and Linux, the default resolver policy permits reads of that companion cache but denies writes to it and the shared Console cache parent.

Console overrides cache-location variables inherited from the host or supplied in `resolver.environment` and `sandbox.environment`.
uv's cache, Python installations, tools, and executable links use `uv/`; IR uses `ir/`; renv uses `renv/`; R's package cache base is the Console root, including `R/reticulate` and `R/pkgcache`.
DuckDB uses `duckdb/extensions`, Matplotlib uses `matplotlib`, and Python's bytecode cache uses `python/bytecode`.
Managed Python also redirects its user base to `python/user`.
An explicit `python` or `RETICULATE_PYTHON` selection preserves the inherited or configured `PYTHONUSERBASE` so preinstalled user-site packages remain importable.
This preserves reads without adding resolver write grants for host package locations.
`RENV_PATHS_CACHE`, `RENV_PATHS_SOURCE`, and `RENV_PATHS_BINARY` are redirected explicitly so an inherited override cannot share host artifacts.
Worker Matplotlib and general XDG caches retain their private temporary storage and read prepared font caches from the captured location.
Workers link the warmed font cache into their private Matplotlib directory.
Host Matplotlib configuration remains selected independently of this cache.
Font-cache warmup selects the persistent cache explicitly so a read-only host configuration directory cannot redirect it to temporary storage.
Explicitly selected Python uses its preinstalled packages and DuckDB extensions.
Host R and installed resolver executables remain readable.
On macOS, keep uv on `PATH` or select an installed executable with `RETICULATE_UV`.
Cold reticulate uv bootstrap uses a shell installer whose temporary files are not confined to `TMPDIR`; the resolver's write policy rejects those host writes.

To reuse host installations and cache settings, set:

```yaml
cache: host
```

Or launch with `mcp-console serve -c cache=host`.
Custom workers that need no dependency preparation can use this setting to start without `HOME` or `XDG_CACHE_HOME`.
This retains resolver sandboxing on macOS/Linux while restoring host cache selection and its default write grants.
`serve --no-sandbox` defaults to host caches; `cache: console` with `--no-sandbox` is rejected to avoid executing Console cache artifacts in a session that disables sandboxing.
Windows redirects cache paths but still prepares dependencies with host permissions.

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
The Console cache root is created even with explicit filesystem entries.
Create custom writable directories before launch on Linux, where absent roots cannot be bound.

With `cache: console`, the default resolver write grant covers only the Console cache root, in addition to private temporary storage.
Explicit `filesystem.entries` must include that root when preparation needs persistent writes.
With `cache: host`, default cache grants cover the locations and direct environment overrides listed below.
Default resolver cache grants include metadata directories such as `.git`, so preparation can populate complete dependency checkouts without workspace metadata masks in shared caches.
Console does not inspect uv configuration files to discover additional writable paths.
In Console cache mode, the captured `UV_CACHE_DIR` overrides a config-file `cache-dir`.
In host cache mode, if `UV_CONFIG_FILE` or `uv.toml` selects a custom `cache-dir`, set the matching `resolver.environment.UV_CACHE_DIR` or grant that path in `resolver.filesystem.entries`.
Use the expanded default as the starting point when supplying entries, since they replace all default grants.
Other storage settings and package sources may also need explicit filesystem or proxy permissions.

For a custom uv cache with an automatic write grant:

```yaml
cache: host
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
Configuration and cache paths are captured at Console startup and retained across preparation calls and worker restarts.
Changes to a running worker's environment do not reconfigure the resolver.
Native resolver policy fields apply only to local sandboxed preparation on macOS/Linux.
Windows local sandboxed sessions apply resolver environment settings without native resolver enforcement.

For a host DuckDB cache at a different path:

```yaml
cache: host
resolver:
  environment:
    MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY: /home/alice/package-caches/duckdb
```

Console captures this path for installation and the managed R/Python SQL connections.
In host cache mode without this override, both use `.duckdb/extensions` beneath the resolver's effective `HOME`, even when it differs from the server or worker `HOME`.
Custom workers receive the same captured path at launch, before accepting their first managed R layer.
User-selected Python retains DuckDB's own cache settings and preinstalled extensions.
An explicit `filesystem.entries` must grant writes to the selected directory.

## Expanded default

For a Linux account with `HOME=/home/alice` and no cache overrides, the following spells out the default permissions.
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
      - path: {type: path, path: /home/alice/.cache/mcp-console/dependencies}
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
  # Console supplies the cache environment described above.
  environment: {}
```

## Host cache selection

With `cache: host`, cache paths and write grants follow the resolver's effective environment:

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
When uv's cache, Python installation, and tool directories are not all explicitly selected, macOS additionally permits uv's legacy `$HOME/Library/Caches/uv` and `$HOME/Library/Application Support/uv` locations.
Host cache mode on macOS also grants Darwin's user temporary directory, which `mktemp` and shell here-documents select independently of `TMPDIR`.
Existing uv and reticulate cache selection remains unchanged in host cache mode.
For uv grants, relative `XDG_CACHE_HOME` and `XDG_DATA_HOME` values are ignored in favor of the defaults beneath `HOME`.
Default host cache grants require an absolute `HOME`.
Selected cache paths must be UTF-8, as required by the native policy protocol.
Host cache selection lives in [`src/resolver/cache.rs`](../src/resolver/cache.rs), shared by resolver grants and captured DuckDB runtime paths.

## Trust boundary

Package code can read host files and modify the granted caches.
Console cache mode separates prepared artifacts from the caches used by ordinary host uv, R, and DuckDB processes.
It does not make artifacts immutable, validate package code, or protect readable secrets.
Host cache mode allows preparation to modify artifacts that other host processes may later execute.
A custom policy can widen these permissions.
Use trusted requirements, resolvers, configuration, and package sources.
The [native runner's lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits) also apply to the resolver.
