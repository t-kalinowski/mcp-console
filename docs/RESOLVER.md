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

Console overrides cache-location variables inherited from the host or supplied in `resolver.environment` and top-level `environment`.
uv's cache, Python installations, tools, and executable links use `uv/`; IR uses `ir/`; renv uses `renv/`; R's package cache base is the Console root, including `R/reticulate` and `R/pkgcache`.
DuckDB uses `duckdb/extensions`, Matplotlib uses `matplotlib`, and Python's bytecode cache uses `python/bytecode`.
Managed Python also redirects its user base to `python/user`.
An explicit `python` or `RETICULATE_PYTHON` selection preserves the inherited or configured `PYTHONUSERBASE` so preinstalled user-site packages remain importable.
This preserves reads without adding resolver write grants for host package locations.
`RENV_PATHS_CACHE`, `RENV_PATHS_SOURCE`, and `RENV_PATHS_BINARY` are redirected explicitly so an inherited override cannot share host artifacts.
Worker Matplotlib and general XDG caches retain their private temporary storage and read prepared font caches from the captured location.
Workers link the warmed font cache into their private Matplotlib directory.
Valid prepared font caches are reused across dependency activation and worker restarts.
An absent or invalid worker cache can trigger construction; worker imports forward Matplotlib's own delayed diagnostic when construction takes several seconds.
Replacing a worker-private cache does not update the prepared source.
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

For a different package mirror:

```yaml
resolver:
  environment:
    UV_INDEX_URL: https://packages.example.org/simple
  sandbox:
    network:
      proxy:
        domains:
          allow: [packages.example.org, artifacts.example.org]
```

Include every host serving artifacts or redirects, plus the R and DuckDB sources needed by the session.
An explicit domains mapping replaces the generated download allowlist; omitted allow/deny lists are empty.
`domains: {}` or `domains: {allow: []}` allows no proxy destinations.
Omitting domains retains the default hosts listed below.

`resolver.sandbox` uses exactly the same [public sandbox schema](SANDBOX_CONFIGURATION.md) as the worker.
Its permissions are independent of `sandbox` and worker writable roots.
Explicit `network: restricted` or `network: enabled` selects no proxy, including after CLI layering.
Omitted networking keeps the managed download proxy on macOS/Linux.
A managed mapping defaults to full mode, TCP SOCKS, no upstream proxy, an empty socket allowlist, and local binding enabled.
Limited mode selects both native SOCKS flags as false; explicit SOCKS fields are errors.
Limited mode blocks HTTPS CONNECT, so normal HTTPS dependency downloads are unavailable under that selection.

Top-level environment values are shared with preparation, including credentials.
`resolver.environment` overrides individual shared values.
`resolver.inherit_environment` defaults to top-level `inherit_environment`, which defaults to true.
Disabling inheritance keeps explicit environment values.
Console's existing runtime, cache, and transport adjustments take precedence.
Settings and cache locations are captured at startup and retained across preparation calls and worker restarts; worker environment mutations do not reconfigure the resolver.
Python selection remains the server's `python` setting or launch `RETICULATE_PYTHON`; workload overrides do not replace that choice.

An omitted resolver filesystem permits host reads and writes to the selected caches and private temporary storage.
An explicitly supplied filesystem mapping replaces all default entries.
Include necessary reads and cache writes; Console does not augment explicit entries automatically.
For example, with host caches:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
  sandbox:
    filesystem:
      read_only: [/]
      read_write: [/home/alice/package-caches/uv]
```

That example grants only the selected uv cache; Python installations, R preparation, and DuckDB may need additional writable directories.
Create custom writable directories before Linux launch; Console creates only its existing default caches.
Paths resolve against the fixed workspace without `~`, variable expansion, symlink rewriting, or parent grants.
The Console cache root is created even with an explicit filesystem mapping, but creation does not grant the resolver access to it.
Default resolver grants keep the native metadata write protections at each cache root; they do not grant its `.git`, `.agents`, or `.codex` children separately.
uv's Git repositories and build metadata live in nested cache directories and use the ordinary cache-root grant.

To select a custom uv cache and retain automatic host-cache grants, omit the filesystem:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
```

Console does not inspect uv configuration files to discover additional writable paths.
In Console cache mode, captured `UV_CACHE_DIR` overrides a config-file `cache-dir`.
In host cache mode, if uv configuration selects a different cache, set the corresponding environment value or grant it explicitly.

For a host DuckDB cache:

```yaml
cache: host
resolver:
  environment:
    MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY: /home/alice/package-caches/duckdb
```

Console captures this path for installation and managed R/Python SQL connections.
Without this override in host cache mode, both use `.duckdb/extensions` beneath the resolver's effective `HOME`.
Sandboxed preparation requires an absolute resolver `HOME` or an explicit `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY`, including when explicit filesystem entries replace the defaults.
If environment inheritance is disabled, provide these values through the shared or resolver environment mappings.
Custom workers receive the captured path before their first managed R layer.
User-selected Python retains DuckDB's own settings and preinstalled extensions.
Explicit filesystem entries must grant writes to the selected directory.

Windows preparation retains host permissions and Job lifecycle.
Explicit `resolver.sandbox` requests are rejected before preparation rather than accepted without enforcement.
Environment and cache settings still apply.
`serve --no-sandbox` likewise rejects explicit sandbox settings while applying workload environment settings with host caches.

## Expanded public default

On macOS/Linux, the following spells out the resolver's default networking.
Omit `resolver.sandbox.filesystem` to retain generated host reads and cache grants.
Console owns private temporary storage, lifecycle, runtime/cache variables, and the macOS extension.
This example is not the omitted Windows resolver policy.

```yaml
resolver:
  inherit_environment: true
  environment: {}
  sandbox:
    network:
      proxy:
        mode: full
        socks5: tcp
        allow_upstream_proxy: false
        domains:
          allow:
            - pypi.org
            - files.pythonhosted.org
            - astral.sh
            - releases.astral.sh
            - github.com
            - api.github.com
            - codeload.github.com
            - raw.githubusercontent.com
            - objects.githubusercontent.com
            - release-assets.githubusercontent.com
            - r-lib.github.io
            - packagemanager.posit.co
            - rspm-sync.rstudio.com
            - bioconductor.posit.co
            - bioconductor.org
            - cran.r-project.org
            - cloud.r-project.org
            - cran.rstudio.com
            - extensions.duckdb.org
      sockets:
        unix_sockets: []
      allow_local_binding: true
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
| Matplotlib          | `MPLCONFIGDIR`, otherwise `$HOME/.matplotlib` on macOS or `${XDG_CACHE_HOME:-$HOME/.cache}/matplotlib` on Linux                                          |

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
