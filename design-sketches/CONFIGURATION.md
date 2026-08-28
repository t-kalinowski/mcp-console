# `.mcp-console/config.yaml`

**Status:** aspirational design sketch

This document proposes a configuration format for MCP Console. It is intended to
remain useful as MCP Console grows from one local macOS sandbox into a
cross-platform system that can run locally, over SSH, in containers, in Docker
Sandboxes, or through another sandbox provider.

The central design rule is:

> **Simple forms are shorthands for advanced forms, not a separate dialect.**

A new user should be able to start with two or three lines, expand only the node
they need, and keep the rest of the file unchanged. Advanced users should still
be able to configure the uncommon details without escaping into an unrelated
configuration system.

## First contact

The smallest useful project file is:

```yaml
version: 1
profile: workspace_write
```

This selects the built-in `workspace_write` profile: the project workspace is
writable, other host writes are blocked, and network access is disabled.

A typical customized file is still small:

```yaml
version: 1

profile:
  extends: workspace_write
  filesystem:
    writable_roots: ~/shared-data
    unreadable:
      - ~/.ssh
      - "**/.env"
```

The meaning should be visible without reading a schema:

- start from `workspace_write`;
- also permit writes under `~/shared-data`;
- deny reads from `~/.ssh` and files named `.env`.

There is no generic `access: deny`. Every permission says whether it affects
reads, writes, outbound connections, listeners, or local sockets.

Narrow network access is one local expansion:

```yaml
profile:
  extends: workspace_write
  network:
    - https://pypi.org
    - https://*.pythonhosted.org
    - tcp://warehouse.example.com:5432
```

A sequence under `network` is shorthand for an outbound allowlist. Everything
not listed remains blocked.

Running remotely is also a local expansion:

```yaml
profile:
  extends: workspace_write
  run_on: ssh://lab-gpu
```

When more SSH detail is needed, only `run_on` expands:

```yaml
profile:
  extends: workspace_write
  run_on:
    type: ssh
    host: lab-gpu
    directory: ~/work/analysis
```

There is no separate transport, compute, bootstrap, or installation graph in
the common form. SSH means that MCP Console starts the relay and worker on that
host. The remote host is resolved through normal OpenSSH configuration and must
already have a compatible `mcp-console` available.

## The learning path

The format is designed around six gradual steps.

### 1. Select a built-in profile

```yaml
version: 1
profile: read_only
```

### 2. Expand the selected profile to change one thing

```yaml
version: 1
profile:
  extends: workspace_write
  filesystem:
    unreadable: ~/.aws
```

Fields that naturally hold one or more values accept either a scalar or a
sequence. Adding a second denial changes only that field:

```yaml
filesystem:
  unreadable:
    - ~/.aws
    - ~/.ssh
```

### 3. Name profiles when there is more than one

```yaml
version: 1
profile: local

profiles:
  local:
    extends: workspace_write

  remote:
    extends: local
    run_on: ssh://lab-gpu
```

The top-level `profile` is either:

- the name of a built-in or user-defined profile; or
- an inline profile mapping.

A project with only one configuration never needs a `profiles` mapping. A
named profile may itself use the scalar shorthand:

```yaml
profiles:
  local: workspace_write
```

This is exactly equivalent to `local: { extends: workspace_write }`.

### 4. Expand a shorthand node without changing its role

```yaml
run_on: ssh://lab-gpu
```

becomes:

```yaml
run_on:
  type: ssh
  host: lab-gpu
  directory: ~/work/analysis
```

Likewise:

```yaml
python:
  - pandas
  - pyarrow
```

becomes:

```yaml
python:
  mode: managed
  packages:
    - pandas
    - pyarrow
  indexes:
    - https://pypi.org/simple
```

### 5. Use patch forms only when modifying inherited collections

A scalar or sequence is the ordinary complete value for that profile. A mapping
with `add`, `remove`, or `replace` is available when inheritance makes a local
patch clearer:

```yaml
profiles:
  corporate:
    extends: workspace_write
    network:
      allow:
        - https://packagemanager.corp.example
        - https://pypi.corp.example

  corporate_with_warehouse:
    extends: corporate
    network:
      allow:
        add: tcp://warehouse.corp.example:5432
```

A user encounters this form only after they are already using profile
inheritance.

### 6. Extract reusable fragments only after repetition appears

A larger user or managed installation may eventually repeat the same policy,
remote runner, package source, or environment across otherwise different
profiles. That is the point at which `definitions` becomes useful:

```yaml
definitions:
  runners:
    lab_gpu:
      type: ssh
      host: lab-gpu
      directory: ~/work/analysis

profiles:
  remote:
    extends: workspace_write
    run_on:
      use: lab_gpu
```

The named object is explicit through `use`. A small configuration never needs a
registry, and extracting a fragment does not change its underlying schema.

## Progressive forms

The schema should use scalar, sequence, and mapping forms only when their
expansions are unambiguous.

| Node | Compact form | Expanded meaning |
| --- | --- | --- |
| `profile` | `workspace_write` | Select the built-in profile. |
| profile entry | `workspace_write` | `{ extends: workspace_write }` |
| `network` | `none` | No outbound network, listeners, or local sockets. |
| `network` | one endpoint or a sequence | `{ mode: allowlist, allow: [...] }` |
| `run_on` | `ssh://lab-gpu` | `{ type: ssh, host: lab-gpu }` |
| `r` or `python` | sequence of packages | Managed environment with those packages. |
| `r` or `python` | `project` | Project environment rooted at `.`. |
| `r` or `python` | `false` | Disable that language. |
| `sql` | `memory` | In-memory DuckDB. |
| `sql` | a path | Persistent DuckDB at that path. |
| `logs` | a path | `{ directory: path }` |
| `cache` | a path | Put MCP Console-managed caches under that root. |
| list-valued field | one scalar | A one-element sequence. |
| inherited list-valued field | `{ add: ..., remove: ... }` | Patch the inherited value. |

### Normalization contract

Progressive forms are useful only when expansion is predictable. The schema
therefore follows these rules:

1. **Expansion is local.** Expanding `run_on` does not require restructuring the
   profile or introducing a target registry.
2. **Every shorthand has one canonical expansion.** A scalar never means one
   thing in a small file and another after `definitions` is added.
3. **References are visibly references.** Named fragments use `{use: name}`; an
   ordinary scalar remains an ordinary host, path, package, or built-in name.
4. **Sequences are complete values.** They replace an inherited sequence. Only
   an explicit `{add, remove}` mapping patches a parent.
5. **Promotion preserves meaning.** Adding detail converts a scalar to a mapping
   around the same value rather than moving it to a different part of the file.
6. **Reserved scalar words are few and documented.** Values such as `none`,
   `full`, `local`, `managed`, `project`, and `memory` are shorthands. A literal
   path or name that collides with one uses the expanded mapping, such as
   `sql: {type: duckdb, path: ./memory}`.

For example, one outbound endpoint may begin as:

```yaml
network: https://pypi.org
```

Adding another endpoint changes only the scalar to a sequence:

```yaml
network:
  - https://pypi.org
  - https://*.pythonhosted.org
```

Adding a listener promotes the same node to a mapping:

```yaml
network:
  allow:
    - https://pypi.org
    - https://*.pythonhosted.org
  listen: 3838
```

All three forms normalize to the same network-policy model.

Every accepted form is normalized before validation. `mcp-console config
explain` should print the normalized form, including defaults and derived
permissions.

Strings should not be overloaded when their meaning would be ambiguous. For
example, `run_on` uses URI-like prefixes for compact remote/container forms,
while an exact executable path is available only in the expanded mapping.

## Core model

A **profile** describes one complete way to run a console session:

- its filesystem, network, listener, and local-socket permissions;
- where its relay and worker run;
- which sandbox provider enforces the policy;
- its R, Python, and SQL environments;
- package-resolution behavior and package sources;
- resource limits, priority, environment variables, logs, and caches.

The common file remains self-contained. Separate permission, target, compute,
and environment registries are not required to understand a profile.

The top-level shape is:

```yaml
version: 1
profile: workspace_write
profiles: {}
definitions: {}
server: {}
logs: {}
cache: {}
```

Only `version` is required. With no configuration file, MCP Console should use
`read_only` and its default managed runtime. `definitions` is optional and is
intended for larger configurations after repetition appears. Unknown fields and
duplicate YAML keys are errors.

A CLI selection overrides the configured default:

```text
mcp-console serve --profile remote
```

Changing profile replaces the worker generation. It does not mutate the
permissions of a running worker in place.

## Built-in profiles

MCP Console ships three user-facing profiles.

### `read_only`

- Ordinary host files are readable unless explicitly denied.
- User-visible host writes are denied.
- Private session scratch space remains writable.
- Outbound network, listener ports, and local sockets are denied.
- The sandbox provider is selected automatically.

### `workspace_write`

The same as `read_only`, plus write access to the profile working directory.
For a project-local configuration, that is the parent of `.mcp-console`. For
SSH or container execution, it is the target-side working directory.

### `full_access`

MCP Console does not apply its portable filesystem or network restrictions in
the selected execution environment. This is the explicit no-sandbox profile.
It must never be selected as an automatic fallback.

These baselines correspond conceptually to Codex's user-facing
`:read-only`, `:workspace`, and `:danger-full-access` profiles. MCP Console uses
`read_only`, `workspace_write`, and `full_access` because the YAML schema already
reserves built-in names; a punctuation prefix adds syntax without resolving an
ambiguity. `workspace_write` also states the granted capability directly.

The names cannot be redefined. The more cautionary name
`danger_full_access` remains a reasonable alternative; see
[Open questions](#open-questions).

## Profile shape

The fully expanded shape is intentionally broad, but most profiles set only a
few fields:

```yaml
profiles:
  example:
    description: Example analysis environment
    extends: workspace_write

    use:
      policy: corporate
      environment: corporate

    run_on: local
    sandbox: auto
    filesystem: {}
    network: none

    package_resolution: automatic
    r: managed
    python: managed
    sql: memory

    resources: {}
    env: {}
    logs: {}
    cache: {}
```

A profile may have one parent. One-parent inheritance keeps security precedence
predictable. Most composition is achieved by expanding the relevant node rather
than combining several opaque parents.

`use` is optional. It imports reusable fragments from `definitions`; direct
fields in the profile remain authoritative over imported fragments. The common
case omits it entirely.

## Filesystem permissions

The common filesystem form uses descriptive path sets:

```yaml
filesystem:
  # Additional writable directory roots.
  writable_roots: ~/shared-output

  # Paths or globs that cannot be read.
  unreadable:
    - ~/.ssh
    - ~/.aws
    - "**/.env"

  # Paths or globs protected from writes.
  unwritable:
    - .git/hooks
    - .git/config
```

`writable_roots`, `unreadable`, and `unwritable` accept one scalar, a sequence,
or a list patch.

`workspace_write` already grants the working directory. `writable_roots` adds
other recursive roots; users do not need to restate the workspace.

Stronger read isolation uses `readable_roots`:

```yaml
filesystem:
  readable_roots:
    - .
    - /datasets/public
  unreadable: "**/.env"
```

When `readable_roots` is present, ordinary user-data reads are limited to those
roots. MCP Console still grants the runtime executables, shared libraries,
package trees, certificates, device nodes, and implementation files required to
start the selected runtime. `config explain` must show those derived runtime
grants separately from user-declared roots.

Relative paths are resolved from the worker's target-side working directory.
`~` is the target user's home. Container paths are container paths; SSH paths
are remote paths. Log paths are an exception and are supervisor-side unless an
expanded log destination says otherwise.

Simple permission precedence is fixed:

- `unreadable` overrides readable roots;
- `unwritable` overrides the workspace grant and `writable_roots`;
- list order does not change authority.

The implementation must resolve symlinks and hard links safely. A backend that
cannot enforce a selected path or glob rejects the profile rather than silently
widening it.

### Patching inherited path sets

```yaml
filesystem:
  writable_roots:
    add:
      - /scratch
    remove:
      - ~/old-output
```

The general list-patch form is:

```yaml
some_list:
  add: []
  remove: []
```

or:

```yaml
some_list:
  replace: []
```

`replace` cannot be combined with `add` or `remove`. A lower-trust project file
cannot remove a denial imposed by a user, administrator, or organization policy
layer.

### Advanced rule form

Some environments need a broad denial with a narrow reopening. The advanced
form expresses that case without making YAML order part of the security policy:

```yaml
filesystem:
  rules:
    - root: /datasets
      read: deny

    - root: /datasets/public
      read: allow
      write: deny

    - root: /datasets/public/output
      write: allow
```

A rule has exactly one matcher: `path`, `root`, or `glob`. `read` and `write`
accept `allow`, `deny`, or `inherit`; at least one must be present. An optional
`roots` field scopes a relative matcher to `workspace`, `home`, or an explicit
list of roots.

The simple fields normalize into the same rule model:

- `readable_roots` produces recursive `read: allow` rules;
- `writable_roots` produces recursive read/write allow rules;
- `unreadable` produces read-deny rules; and
- `unwritable` produces write-deny rules.

Simple fields and explicit `rules` may therefore coexist. Authorization is
resolved independently for reads and writes:

1. the most specific matching rule wins;
2. at equal specificity, `deny` wins over `allow`;
3. effective write access requires effective read access; and
4. administrator or organization constraints remain non-overridable.

Rule sequence order has no authorization meaning. A stable optional `id` may be
used to replace or remove an inherited advanced rule through the list-patch
form.

The advanced form is a long-tail feature. It does not need to be implemented in
the first configuration PR, but reserving the shape avoids redesigning the
filesystem block when read reopening or richer matcher metadata is needed.

## Network permissions

The whole network node has useful compact forms:

```yaml
network: none
```

```yaml
network: https://cran.r-project.org
```

```yaml
network:
  - https://cran.r-project.org
  - https://*.pythonhosted.org
  - tcp://warehouse.example.com:5432
```

```yaml
network: full
```

The built-in profiles begin with an empty outbound allowlist. A scalar endpoint
or sequence grants only the listed destinations. `full` allows unrestricted
outbound networking but does not implicitly grant filesystem access, listener
ports, or local IPC sockets; those remain separate capabilities. It is still a
powerful setting requiring a trusted profile.

Writing `network: none` in a selected profile is stronger than merely inheriting
the built-in empty allowlist: it is an explicit prohibition on outbound
connections, listeners, and local sockets. It conflicts with sibling settings
that request a derived network grant, such as `sql.allow_network: true` or a
package source with `worker_access: true`. To allow exactly one database or
package source, omit `network: none` and use that explicit narrow grant.

The expanded form is:

```yaml
network:
  mode: allowlist
  allow:
    - https://api.example.com/v1/**
    - tcp://warehouse.example.com:5432

  deny:
    - tcp://*.example.com:22

  listen: 3838

  unix_sockets:
    - /var/run/postgresql/.s.PGSQL.5432

  proxy: managed
```

`mode` is `allowlist`, `full`, or `none`; it is normally omitted because a
mapping with `allow`, `listen`, or `unix_sockets` implies `allowlist`. `allow`,
`deny`, `listen`, and `unix_sockets` accept a scalar, sequence, or list patch.
Denials win independently of list order.

### Outbound endpoint strings

Portable endpoint strings use an explicit scheme:

- `http://host[:port][/path-pattern]`;
- `https://host[:port][/path-pattern]`;
- `tcp://host:port`;
- `udp://host:port`, where the selected provider can enforce it.

DNS wildcards may cover subdomains, such as `*.pythonhosted.org`. Overly broad
patterns such as `*` or `*.com` should be rejected in an allowlist.

A URL path pattern requires request inspection. If a selected provider can
filter only by host, it must reject a path-constrained profile rather than turn
it into a host-wide grant.

An endpoint can expand to a mapping when methods, several ports, or a denial
reason are needed:

```yaml
network:
  allow:
    - url: https://api.example.com/v1/**
      methods: [GET, POST]

    - host: warehouse.example.com
      protocol: tcp
      port: 5432

  deny:
    - endpoint: tcp://github.com:22
      reason: Use an HTTPS Git remote.
```

The simple string and expanded mapping normalize to the same internal endpoint
rule.

### Managed proxy

An allowlist defaults to MCP Console's managed proxy or equivalent mediated
network path:

```yaml
network:
  allow: https://pypi.org
  proxy: managed
```

The advanced proxy form is:

```yaml
network:
  allow:
    - https://*.corp.example
    - https://internal-mtls.example.net

  proxy:
    type: managed
    upstream: http://proxy.corp.example:8080
    tls:
      inspect: auto
      exclude:
        - internal-mtls.example.net
      extra_ca:
        - /etc/corp-roots.pem
```

`upstream` routes the managed proxy through a corporate proxy; it does not
bypass the profile allowlist. TLS inspection is enabled only when required by a
path, method, or credential policy. Exclusions support mTLS or certificate
pinning. Provider-specific proxy listener ports are implementation details and
should not appear in normal configuration.

### Listener ports and local application development

Shiny is not a special service type. It is one program that can use an allowed
listener:

```yaml
profile:
  extends: workspace_write
  network:
    listen: 3838
```

The compact listener form means:

- allow a TCP listener on target-side loopback port `3838`;
- publish it on supervisor-side loopback port `3838`;
- create SSH forwarding or a container mapping when necessary;
- never expose it on a non-loopback interface.

The worker may then run:

```r
shiny::runApp(host = "127.0.0.1", port = 3838)
```

A listener expands when the target and published ports differ or an automatic
port is needed:

```yaml
network:
  listen:
    - port: 3838
      protocol: tcp
      publish: loopback
      local_port: 8383

    - name: preview
      port: auto
      protocol: tcp
      publish: loopback
      env: MCP_CONSOLE_PREVIEW_PORT
```

For an automatic port, MCP Console reserves the target port, exposes it through
the named environment variable, and reports the published URL. If an exact
published port is occupied, startup fails clearly.

### Local sockets

Exact Unix-domain socket paths use:

```yaml
network:
  unix_sockets: /var/run/postgresql/.s.PGSQL.5432
```

An expanded entry can distinguish connecting from binding when a provider
supports it:

```yaml
network:
  unix_sockets:
    - path: /var/run/postgresql/.s.PGSQL.5432
      operations: [connect]
```

Windows named pipes can later use a parallel `named_pipes` field. High-authority
sockets such as `/var/run/docker.sock` require explicit project trust.

## Where the relay and worker run

`run_on` answers one question: **where should MCP Console start this session's
relay and worker?**

### Compact forms

```yaml
run_on: local
```

```yaml
run_on: ssh://lab-gpu
```

```yaml
run_on: "docker://ghcr.io/acme/analysis:2026.08"
```

```yaml
run_on: "docker-sandbox://ghcr.io/acme/analysis:2026.08"
```

The scalar forms intentionally contain only the identity needed for the common
case. `ssh://lab-gpu` starts in the remote account's home directory; a project
profile normally expands the node and sets `directory`. The compact Docker forms
mount the project at `/workspace` and use that as the working directory.

### SSH

```yaml
run_on:
  type: ssh
  host: lab-gpu
  directory: ~/work/analysis
```

`host` is an OpenSSH host or alias. User, port, identity file, jump host,
host-key policy, and keepalive normally remain in `~/.ssh/config`.

The remote must already contain the project and a compatible MCP Console. The
default remote command is `mcp-console` from `PATH`. An exact path can be
supplied without introducing an installation subsystem:

```yaml
run_on:
  type: ssh
  host: lab-gpu
  directory: ~/work/analysis
  mcp_console: /opt/mcp-console/bin/mcp-console
```

There is no `bootstrap` block and no `install: require`. Automatic remote
provisioning can be added later under an explicitly named `provision` mapping if
real use requires it; it should not be part of the first SSH experience.

Optional project synchronization is also explicit rather than implied:

```yaml
run_on:
  type: ssh
  host: lab-gpu
  directory: ~/work/analysis
  sync:
    type: rsync
    source: .
    exclude:
      - .git
      - .mcp-console/cache
```

Version 1 may reasonably require the remote directory to exist and defer
`sync`.

### Docker

```yaml
run_on:
  type: docker
  image: ghcr.io/acme/analysis:2026.08
  directory: /workspace
```

The compact Docker form defaults to mounting the project at `/workspace` and
using it as the working directory. The expanded form can configure mounts and
small runtime arguments:

```yaml
run_on:
  type: docker
  image: ghcr.io/acme/analysis:2026.08
  directory: /workspace
  mounts:
    - source: .
      target: /workspace
      mode: read_write
    - source: /datasets/public
      target: /datasets/public
      mode: read_only
  args:
    - --gpus=all
```

Image builds should normally use a separate Dockerfile:

```yaml
run_on:
  type: docker
  build:
    context: .
    dockerfile: .mcp-console/Dockerfile
  directory: /workspace
```

Embedding a full Dockerfile in YAML is possible in principle but is not
recommended. It harms readability, editor support, caching, and reuse.

### Docker Sandbox

```yaml
run_on:
  type: docker_sandbox
  image: ghcr.io/acme/analysis:2026.08
  directory: /workspace
```

This records the intended execution product without exposing its microVM,
daemon, or forwarding internals.

### Composing SSH with a container

The common design avoids separate transport and compute graphs, but one nested
`inside` node covers a real composition without making every user assemble
axes:

```yaml
run_on:
  type: ssh
  host: lab-gpu
  directory: ~/work/analysis
  inside:
    type: docker
    image: ghcr.io/acme/analysis-gpu:2026.08
    directory: /workspace
    args:
      - --gpus=all
```

MCP Console connects to the host, starts the container there, and runs the relay
and worker in the innermost environment. Listener forwarding and derived paths
cross both boundaries. Additional arbitrary nesting should not be added until a
concrete supported use case requires it.

### Arbitrary execution provider

A company-specific remote execution system can use an advanced command runner:

```yaml
run_on:
  type: command
  command:
    - /opt/acme/bin/remote-run
    - --pool
    - data-science
    - --
  directory: /workspace
  protocol: relay_stdio
  env:
    ACME_PROJECT: analytics
```

MCP Console appends its internal relay command after the command prefix and does
not invoke an implicit shell. `relay_stdio` means the provider preserves the
relay's framed standard-input and standard-output contract.

This is an escape hatch, not the common remote-host model.

## Sandbox provider

The built-in profile describes portable permissions. The optional `sandbox`
node selects how those permissions are enforced in the environment chosen by
`run_on`.

The default is:

```yaml
sandbox: auto
```

It expands to:

```yaml
sandbox:
  provider: auto
```

`auto` chooses the best supported provider on the target. A selected security
property that the provider cannot enforce is an error.

A known provider may be requested for diagnostics, compatibility, or an
organization policy:

```yaml
sandbox:
  provider: codex
```

```yaml
sandbox:
  provider:
    type: srt
    options:
      enable_weaker_nested_sandbox: true
```

Portable filesystem and network behavior remains outside `options`.
Provider-specific options are explicitly nonportable and should be rare.

A third-party sandbox wrapper is:

```yaml
sandbox:
  provider:
    type: command
    command:
      - /opt/acme/bin/sandbox-run
      - --profile
      - data-science
      - --
```

MCP Console appends the relay command. Without a policy contract, the external
sandbox owns the entire isolation boundary, so the profile must extend
`full_access`; MCP Console cannot claim that its portable rules were enforced.

A provider that can consume compiled MCP Console policy may declare the
contract explicitly:

```yaml
sandbox:
  provider:
    type: command
    command:
      - /opt/acme/bin/sandbox-run
    policy:
      format: mcp-console/v1
      argument: --policy
      capabilities:
        - filesystem
        - network
        - resources
```

MCP Console writes a generated policy file, passes its path through the declared
argument, and verifies the provider's advertised capabilities. This preserves a
portable top-level policy while allowing a long tail of enforcement backends.

`run_on` and `sandbox` are separate because they answer different questions:
where the session runs, and what enforces its permissions there. They remain
composable without exposing separate transport, compute, bootstrap, and relay
objects.

## Package resolution and language environments

The profile-wide package policy is compact:

```yaml
package_resolution: automatic
```

Supported values are:

- `automatic` — prepare declared packages and permit supported runtime-triggered
  resolution;
- `declared_only` — prepare project manifests and explicitly listed packages,
  but never install merely because evaluated code tries to load or import a
  missing package;
- `off` — perform no installation or managed environment construction.

The policy can expand when R and Python differ:

```yaml
package_resolution:
  default: declared_only
  r: automatic
  python: off
```

A future `frozen` value may require a complete lockfile and prohibit any
resolution that would change it.

### R

Compact R forms are:

```yaml
r: managed
```

```yaml
r: project
```

```yaml
r:
  - tidyverse
  - arrow
```

```yaml
r: false
```

A package sequence means a managed R environment with those packages available
from startup. Packages are not attached automatically.

Managed R expands to:

```yaml
r:
  mode: managed
  version: "4.5"
  packages:
    - tidyverse
    - arrow
  manifest: .mcp-console/r-packages.yaml
  repos:
    - https://cran.r-project.org
```

A managed environment may also set `lifetime: session` for a fresh
ephemeral library or `lifetime: cached` for a manifest-keyed reusable library.
The default should be `cached`; the cache remains disposable and is not treated
as project state.

A project environment is:

```yaml
r:
  mode: project
  root: .
  manager: auto
```

`manager: auto` may detect an `ir` manifest, `renv.lock`, `DESCRIPTION`, or other
supported project metadata and must report what it selected.

An exact existing installation is:

```yaml
r:
  mode: existing
  executable: /opt/R/4.5.1/bin/R
  libraries:
    - /opt/R/4.5.1/site-library
```

With `package_resolution: off`, MCP Console uses it without constructing or
modifying a library.

### Python

Python uses parallel forms:

```yaml
python: managed
```

```yaml
python: project
```

```yaml
python:
  - numpy
  - pandas
  - polars
```

```yaml
python: false
```

The expanded managed form is:

```yaml
python:
  mode: managed
  version: "3.13"
  packages:
    - numpy
    - pandas
    - polars
  manifest: .mcp-console/python-requirements.txt
  indexes:
    - https://pypi.org/simple
```

As with R, `lifetime: session` requests an ephemeral environment and
`lifetime: cached` requests a reusable manifest-keyed environment.

A project environment is:

```yaml
python:
  mode: project
  root: .
  manager: auto
```

An exact existing interpreter is:

```yaml
python:
  mode: existing
  executable: .venv/bin/python
```

Because MCP Console uses reticulate for object translation, the selected Python
must also be compatible with the runtime architecture used by the worker.
Validation should report incompatibility before the session starts.

### Package sources and corporate mirrors

The compact source form is a scalar or sequence:

```yaml
r:
  mode: managed
  repos: https://packagemanager.corp.example/cran/approved

python:
  mode: managed
  indexes: https://pypi.corp.example/simple
```

An expanded source can carry credentials and worker-access intent without
putting secrets in YAML:

```yaml
python:
  mode: managed
  indexes:
    - url: https://pypi.corp.example/simple
      name: approved
      credentials_env: CORP_PYPI_TOKEN
      worker_access: false
```

Declaring a source authorizes MCP Console's package resolver to use that source;
it does not automatically give evaluated code general network access.
`worker_access: true` derives the corresponding narrow worker allowlist when a
package manager must run inside the sandbox. Derived access must be shown by
`config explain`.

A larger configuration may extract a source under
`definitions.package_sources` and refer to it explicitly:

```yaml
python:
  mode: managed
  indexes:
    - use: corporate_pypi
      worker_access: false
```

The referenced definition supplies the source URL and type; fields beside `use`
overlay that definition. An unadorned scalar remains a URL and is never treated
as a definition name.

A scalar or sequence of `repos` or `indexes` replaces the inherited source list
rather than appending to it. This prevents a corporate mirror from silently
falling back to a public registry. The list-patch form is available when adding
a secondary source is intentional.

## SQL

Omitted SQL configuration means an in-memory DuckDB database. The compact forms
are:

```yaml
sql: memory
```

```yaml
sql: .mcp-console/data.duckdb
```

```yaml
sql: false
```

A path scalar expands to:

```yaml
sql:
  type: duckdb
  path: .mcp-console/data.duckdb
```

A read-only database is:

```yaml
sql:
  type: duckdb
  path: /datasets/catalog.duckdb
  read_only: true
```

MCP Console derives the exact filesystem grant required by the declared path.
It must not make the containing tree writable merely because a database file is
writable.

A network database is:

```yaml
sql:
  type: postgres
  host: warehouse.corp.example
  port: 5432
  database: analytics
  user: analyst
  password_env: WAREHOUSE_PASSWORD
  allow_network: true
```

`allow_network: true` derives only
`tcp://warehouse.corp.example:5432`. The connection declaration and the network
grant remain visibly linked, but the grant is still explicit. Credentials are
referenced by name and redacted from diagnostics.

DuckDB-specific extensions and an exact driver can expand the same
connection without changing the compact forms:

```yaml
sql:
  type: duckdb
  path: .mcp-console/data.duckdb
  extensions: [json, parquet]
  driver:
    mode: existing
    library: /opt/duckdb/lib/libduckdb.so
```

`driver` is adapter-specific. The portable default is MCP Console's bundled or
managed adapter; an `existing` driver is an advanced, trusted-host selection.

Multiple connections expand the same node:

```yaml
sql:
  default: warehouse
  connections:
    warehouse:
      type: postgres
      host: warehouse.corp.example
      port: 5432
      database: analytics
      user: analyst
      password_env: WAREHOUSE_PASSWORD
      allow_network: true

    scratch:
      type: duckdb
      path: .mcp-console/scratch.duckdb
```

The single-connection form is shorthand for a connection named `default`.
Future adapters such as SQLite, PostgreSQL, and other databases fit under the
same connection mapping without changing the compact DuckDB forms.

## Resource limits and scheduling priority

Simple hard limits are scalar values:

```yaml
resources:
  memory: 8GiB
  cpu: 2
  processes: 256
  priority: background
```

`memory` is a maximum resident-memory budget where enforceable. `cpu: 2` means
at most two logical CPUs or the closest enforceable equivalent. `background`
asks the OS to prefer interactive user work over the agent.

The expanded form supports the long tail:

```yaml
resources:
  memory:
    soft: 6GiB
    max: 8GiB

  cpu:
    cores: 2
    max_percent: 150
    weight: low

  processes: 256
  open_files: 2048
  writable_storage: 20GiB
  max_runtime: 12h

  priority:
    nice: 10
    io: idle
```

Hard limits default to required enforcement. A profile requesting a hard limit
that the selected host/container/provider cannot enforce fails to start.
Scheduling priority is a hint and may produce a visible warning when an exact
cross-platform equivalent does not exist.

Server-wide session concurrency is separate:

```yaml
server:
  max_sessions: 4
```

A future per-profile `max_instances` may further limit expensive profiles such
as a remote GPU environment.

## Logs

A scalar selects the supervisor-side log directory:

```yaml
logs: .mcp-console/logs
```

A scalar duration under `retention` is shorthand for
`retention.max_age`. The expanded form controls several retention bounds:

```yaml
logs:
  directory: .mcp-console/logs
  retention:
    max_age: 14d
    max_size: 2GiB
    max_files: 1000
```

Logs are distinct from session transcripts and generated artifacts. Remote
relay diagnostics are collected into supervisor-side logs unless an advanced
destination explicitly says otherwise. Active session files are never removed
by retention cleanup. Top-level `logs` supplies the default; a `logs` node inside
a named profile overrides it for that profile.

## Caches

A scalar routes MCP Console-managed caches under one root:

```yaml
cache: .mcp-console/cache
```

It expands to per-ecosystem subdirectories chosen by MCP Console. A mapping can
preserve natural host locations selectively:

```yaml
cache:
  r: .mcp-console/cache/r
  python: host
  duckdb: host
  cleanup:
    max_age: 30d
    every: 24h
```

`host` means the ecosystem's normal cache location in the environment where the
worker or resolver runs. A path asks MCP Console to propagate the relevant
environment variables and runtime options.

A cache namespace can expand further when necessary:

```yaml
cache:
  r:
    packages: .mcp-console/cache/r/packages
    downloads: .mcp-console/cache/r/downloads
  python:
    wheels: host
    environments: .mcp-console/cache/python/environments
  duckdb: host
  cleanup:
    max_age: 30d
    max_size: 20GiB
    every: 24h
```

Cleanup is incremental, based on last use, and never removes an active
environment. Remote/cache paths are target-side; the simple top-level log path
is supervisor-side. Top-level `cache` supplies the default and a profile-local
`cache` mapping overrides only the named namespaces it changes.

## Environment variables and secrets

A plain mapping is shorthand for variables to set:

```yaml
env:
  OMP_NUM_THREADS: "2"
  R_MAX_NUM_DLLS: "200"
```

The expanded form controls inheritance and removal. The keys `set`,
`inherit`, `remove`, and `secret` are reserved for this expanded form; an
environment variable with one of those unusual names must be placed under
`set`.

```yaml
env:
  set:
    OMP_NUM_THREADS: "2"
  inherit:
    - HTTP_PROXY
    - HTTPS_PROXY
    - WAREHOUSE_PASSWORD
  remove:
    - AWS_SECRET_ACCESS_KEY
  secret:
    - WAREHOUSE_PASSWORD
```

`secret` marks inherited or set names for mandatory redaction. Secret values
must not appear in project YAML. A future secret-provider form can expand an
entry without changing callers that already reference an environment variable
name.

The default inheritance policy must be conservative, documented, and visible in
`config explain`.

## Inheritance, file layers, and precedence

### Profile inheritance

A user-defined profile has at most one parent:

```yaml
profiles:
  corporate:
    extends: workspace_write
    package_resolution: declared_only

  corporate_remote:
    extends: corporate
    run_on: ssh://lab-gpu
```

Merge rules are predictable:

- scalars replace inherited scalars;
- mappings merge by key;
- a scalar or sequence assigned to a list-valued field replaces that inherited
  field;
- `{ add, remove }` patches an inherited list;
- `{ replace }` explicitly replaces an inherited list;
- `run_on` replaces the complete inherited runner;
- the advanced filesystem `rules` list replaces the inherited list unless it
  uses an explicit list patch;
- provider-specific `options` merge only within the same provider type.

This is more explicit than silently appending every list and safer than a
fully general YAML merge language. Missing and empty remain distinct: an omitted
field inherits or defaults, while an explicit empty sequence means deliberately
empty. YAML `null` is rejected unless a field defines a specific reset meaning.
YAML anchors may reduce text, but YAML merge keys are not a second inheritance
system; `extends`, `use`, and list patches define the configuration semantics.

Sizes and durations use strict values such as `512MiB`, `8GiB`, `250ms`, `30s`,
`24h`, and `30d`. Ambiguous bare units are rejected.

### Configuration sources

The same schema may eventually be loaded from several trust levels:

1. built-in defaults;
2. administrator or organization policy;
3. user configuration;
4. project `.mcp-console/config.yaml`;
5. explicit CLI selection or non-security overrides.

A higher-trust policy may:

- add non-removable read, write, network, or socket denials;
- cap memory, CPU, storage, runtime, or session concurrency;
- forbid `full_access`, SSH, Docker, custom commands, or selected providers;
- restrict package sources and executable paths;
- limit which profiles an agent may select.

A project file cannot weaken those constraints. Security policy should not be
encoded by relying on ordinary profile merge order alone.

### Configuration discovery and path namespaces

For the project-level experience, MCP Console searches from the working
directory toward the filesystem root and uses the nearest
`.mcp-console/config.yaml`. The parent of `.mcp-console` is the project root. An
explicit `--config` path disables that search. User and organization
configuration use platform-appropriate configuration directories rather than a
project checkout.

Every path belongs to a documented namespace:

- **project/supervisor paths** include the config file, Docker build context,
  Dockerfile, mount sources, sync sources, and the compact top-level log path;
- **target paths** include `run_on.directory`, filesystem roots, runtime
  executables and project roots, SQL database paths, and target caches; and
- **container paths** include mount targets and an `inside.directory`.

Relative project/supervisor paths resolve from the project root. Relative target
paths resolve from the target working directory. `~` expands for the relevant
target-side user. For Docker, a mount `source` is transport-host-visible and its
`target` is container-visible. A field that crosses namespaces must name both
sides rather than relying on implicit path rewriting.

`config explain` must label resolved paths as supervisor, remote-host, or
container paths. Diagnostics preserve both the authored and resolved values
without exposing secret contents.

### Derived permissions

Some declarations imply a narrow supporting permission:

- a writable DuckDB path implies access to that exact database and its required
  side files;
- `sql.allow_network: true` implies one exact database endpoint;
- `worker_access: true` on a package source implies access to that source;
- a cache path implies the corresponding cache read/write root;
- a listener implies one target bind and one supervisor-side loopback forward;
- a container mount implies target-side visibility but does not automatically
  make the source writable.

Derived permissions must be explicit in `config explain`, labeled by their
source, and included in enforcement tests. Hidden broad grants are not
acceptable.

## Agent-selectable profiles

Profile switching should be opt-in:

```yaml
server:
  max_sessions: 4
  agent_profiles:
    - read_only
    - local
    - remote
```

An agent may request only listed profiles. `full_access`, custom runners,
powerful sockets, and other high-authority profiles are not agent-selectable
unless explicitly listed by a trusted user configuration.

Switching profile restarts the worker generation and loses in-memory R, Python,
SQL, debugger, and process state. Workspace files, configured requirements, and
the transcript may remain according to normal session lifecycle rules.

## Trust and fail-closed behavior

Project configuration can request powerful behavior. At minimum, these require
an explicitly trusted project or a higher-trust configuration source:

- `full_access` or `network: full`;
- SSH, Docker, Docker Sandbox, nested execution, or custom command runners;
- third-party sandbox providers;
- high-authority Unix sockets or named pipes;
- arbitrary executable paths outside the project;
- inherited secret-bearing environment variables;
- provider-specific options that weaken isolation.

Editing the project file inside a running worker must not increase that
worker's authority. A more permissive resolved profile requires a new worker and
any required trust decision.

Security-relevant unsupported behavior is always an error. Examples include:

- a filesystem glob the target provider cannot enforce;
- a URL path rule without request inspection;
- a requested listener that cannot be forwarded safely;
- a hard resource limit the runner cannot apply;
- a custom provider that does not implement its declared policy contract.

Warnings are appropriate only for non-security hints such as scheduling
priority.

## Explanation and validation commands

The format depends on good introspection:

```text
mcp-console config validate
mcp-console config validate --all
mcp-console config explain
mcp-console config explain --profile remote
mcp-console config capabilities --profile remote
mcp-console config schema
```

Validation is staged:

1. **Schema validation** checks all declarations, references, inheritance, and
   normalized forms without contacting remote targets.
2. **Selected-profile compilation** resolves one effective profile, including
   derived permissions and trust constraints.
3. **Target preflight** checks the actual host, runner, and sandbox provider
   capabilities before starting a worker.

An unsupported profile that is not selected does not prevent another profile
from running. `validate --all` may contact every target and checks each profile
end to end. A selected restriction that cannot be enforced is an error rather
than a best-effort downgrade.

`config explain` should show:

- the selected profile and inheritance chain;
- every shorthand expansion;
- the target host/container and each target-side path;
- explicit and derived filesystem permissions;
- outbound endpoints, listeners, forwards, and local sockets;
- the selected sandbox provider and its advertised capabilities;
- R, Python, SQL, manifests, packages, and package sources;
- resource limits and whether each is hard or advisory;
- log and cache paths in their correct host/target namespaces;
- the source file and trust level that contributed every nondefault value;
- warnings, redactions, and any unenforceable request.

A published JSON Schema should provide editor completion. Because scalar,
sequence, and mapping unions can produce poor generic schema errors, the CLI
validator should also emit purpose-written diagnostics and show the expanded
form that would be accepted.

## Common recipes

### Add one writable root and two unreadable paths

```yaml
version: 1
profile:
  extends: workspace_write
  filesystem:
    writable_roots: ~/shared-data
    unreadable: [~/.ssh, "**/.env"]
```

### No sandbox

```yaml
version: 1
profile: full_access
```

### No network

```yaml
profile:
  extends: workspace_write
  network: none
```

`network: none` is already the built-in default and is shown here only for
explicitness.

### Approved package mirrors with no runtime-triggered installation

```yaml
profile:
  extends: workspace_write
  package_resolution: declared_only

  r:
    mode: managed
    packages: [dplyr, arrow]
    repos: https://packagemanager.corp.example/cran/approved

  python:
    mode: managed
    packages: [pandas, pyarrow]
    indexes: https://pypi.corp.example/simple
```

### Shiny development

```yaml
profile:
  extends: workspace_write
  network:
    listen: 3838
```

### Exact PostgreSQL access

```yaml
profile:
  extends: workspace_write
  sql:
    type: postgres
    host: warehouse.corp.example
    port: 5432
    database: analytics
    password_env: WAREHOUSE_PASSWORD
    allow_network: true
```

### Remote GPU host

```yaml
profile:
  extends: workspace_write
  run_on:
    type: ssh
    host: lab-gpu
    directory: ~/work/analysis
  resources:
    memory: 64GiB
    cpu: 8
    priority: background
```

### Remote GPU container with a listener

```yaml
profile:
  extends: workspace_write
  run_on:
    type: ssh
    host: lab-gpu
    directory: ~/work/analysis
    inside:
      type: docker
      image: ghcr.io/acme/analysis-gpu:2026.08
      directory: /workspace
      args: [--gpus=all]
  network:
    listen: 3838
  resources:
    memory: 64GiB
    cpu: 8
```

## Fuller example

This example uses the advanced forms, but the first profile remains readable
without following references into other registries:

```yaml
version: 1
profile: local

server:
  max_sessions: 3
  agent_profiles: [local, remote_gpu]

profiles:
  local:
    extends: workspace_write

    filesystem:
      writable_roots:
        - ~/shared-data
      unreadable:
        - ~/.ssh
        - ~/.aws
        - "**/.env"
      unwritable:
        - .git/hooks

    network:
      allow:
        - https://api.example.com/v1/**
      listen: 3838

    package_resolution: declared_only

    r:
      mode: managed
      packages: [tidyverse, arrow]
      repos: https://packagemanager.corp.example/cran/approved

    python:
      mode: managed
      packages: [pandas, pyarrow]
      indexes: https://pypi.corp.example/simple

    sql: .mcp-console/analysis.duckdb

    resources:
      memory: 8GiB
      cpu: 2
      priority: background

  remote_gpu:
    extends: local

    run_on:
      type: ssh
      host: lab-gpu
      directory: ~/work/analysis
      inside:
        type: docker
        image: ghcr.io/acme/analysis-gpu:2026.08
        directory: /workspace
        args: [--gpus=all]

    resources:
      memory: 64GiB
      cpu: 8

logs:
  directory: .mcp-console/logs
  retention: 14d

cache:
  r: .mcp-console/cache/r
  python: host
  duckdb: host
  cleanup:
    max_age: 30d
    every: 24h
```

The long-tail configuration is available, but none of it changes the meaning of
the short forms used earlier.

## Optional reusable definitions

The earlier design separated permissions, targets, compute environments,
sandbox providers, and language environments at the top level. That modularity
has real value for teams and managed installations, but it is the wrong first
contact.

The recommended compromise is an optional `definitions` namespace. Complete
profiles remain the public center; reusable fragments are extracted only when
several profiles actually repeat them.

```yaml
definitions:
  policies: {}
  runners: {}
  environments: {}
  package_sources: {}
  connections: {}
```

Definitions are not selectable workloads. They are typed fragments:

- a `policy` contains `filesystem` and `network` fields;
- a `runner` has the same shape as `run_on`;
- an `environment` contains package-resolution, R, Python, and optional SQL
  defaults;
- a `package_source` is one typed CRAN or PyPI source that can be used in an
  inline `repos` or `indexes` list; and
- a `connection` has the same shape as one SQL connection.

A fuller reusable configuration is:

```yaml
version: 1
profile: local

definitions:
  policies:
    corporate:
      filesystem:
        unreadable:
          - ~/.ssh
          - ~/.aws
          - "**/.env"

  runners:
    lab_gpu:
      type: ssh
      host: lab-gpu
      directory: ~/work/analysis

  environments:
    corporate:
      package_resolution: declared_only
      r:
        mode: managed
        packages: [tidyverse, arrow]
        repos:
          - use: corporate_cran
      python:
        mode: managed
        packages: [pandas, pyarrow]
        indexes:
          - use: corporate_pypi

  package_sources:
    corporate_cran:
      type: cran
      url: https://packagemanager.corp.example/cran/approved

    corporate_pypi:
      type: pypi
      url: https://pypi.corp.example/simple

  connections:
    warehouse:
      type: postgres
      host: warehouse.corp.example
      port: 5432
      database: analytics
      password_env: WAREHOUSE_PASSWORD
      allow_network: true

profiles:
  local:
    extends: workspace_write
    use:
      policy: corporate
      environment: corporate
    sql:
      use: warehouse

  remote_gpu:
    extends: local
    run_on:
      use: lab_gpu
    resources:
      memory: 64GiB
      cpu: 8
```

A reference is always explicit. `run_on: ssh://lab-gpu` always means the SSH
host alias; `run_on: {use: lab_gpu}` always means the named runner definition.
The same distinction applies to `sql.use` and profile-level `use`.

Direct fields overlay the referenced fragment, so a small exception remains
local:

```yaml
run_on:
  use: lab_gpu
  directory: ~/work/another-project
```

Each definition may have at most one parent of the same kind. Resolution order
is:

1. the selected profile's parent;
2. referenced policy and environment fragments;
3. referenced runner or connection nodes;
4. direct fields on the selected profile; and
5. higher-trust constraints.

This retains the thorough component model from the earlier design without
requiring ordinary users to resolve five registries before understanding one
profile.

## Tradeoffs and alternatives

### Uniform mappings only

Requiring every node to be a mapping gives a simpler parser and JSON Schema, but
turns `run_on: ssh://lab-gpu` into several lines and makes small project files
look like infrastructure manifests.

**Recommendation:** accept scalar and sequence shorthands only where expansion
is obvious, then normalize immediately.

### Separate permission, target, and environment registries

Registries reduce duplication and permit independent reuse. They also create
several namespaces, references, and precedence relationships before the user
can perform a simple task.

**Recommendation:** keep complete profiles as the public center and support
named fragments only under optional `definitions`, introduced after real-world
repetition appears.

### Multiple profile inheritance

Multiple parents can independently contribute a corporate policy, remote host,
and runtime environment. Merge order then becomes authority order, especially
for allowlists and denials.

**Recommendation:** one parent. Expand or patch the relevant node explicitly.

### General rule objects everywhere

A uniform rule language is maximally expressive, but makes simple roots verbose
and forces every user to learn matcher specificity.

An ordered last-match-wins variant also makes line movement and configuration
merges security-sensitive.

**Recommendation:** use explicit roots and denials for common cases. Normalize
them into an optional advanced rule form whose precedence is based on
specificity, not declaration order.

### Implicit permissions from every declaration

Automatically granting network and filesystem access for package sources,
databases, caches, mounts, and listeners minimizes repetition but can conceal
authority.

**Recommendation:** derive only narrow, obvious support permissions; require an
explicit opt-in such as `allow_network: true` when the declaration itself does
not necessarily imply authority. Always show derived grants in `config
explain`.

### Fully decomposed execution graphs

Independent transport, host, compute, container, sandbox, relay, provisioning,
and forwarding nodes can model any hypothetical topology. Most combinations
will never be supported and the SSH case becomes hard to read.

**Recommendation:** concrete `run_on` variants, one optional `inside` level, and
an advanced command escape hatch.

## Suggested implementation sequence

1. Parse YAML, reject duplicate/unknown keys, normalize scalar/sequence/mapping
   forms, and implement `config validate`, `config explain`, and schema output.
2. Implement `profile`, built-ins, named profiles, one-parent inheritance, and
   list patches.
3. Implement local `writable_roots`, `readable_roots`, `unreadable`, and
   `unwritable`.
4. Implement `network: none`, outbound endpoint allowlists, exact TCP listeners,
   and Unix socket paths through a managed proxy.
5. Implement package policy, compact/expanded R and Python environments, package
   sources, DuckDB paths, logs, caches, and derived-permission reporting.
6. Add optional `definitions` and explicit `{use: ...}` references after direct
   profile forms and normalization are stable.
7. Implement SSH with a preinstalled remote MCP Console and listener forwarding.
8. Implement Docker, Docker Sandbox, enforceable resources, and nested
   SSH-plus-container execution.
9. Implement provider selection, third-party command providers, and the policy
   contract.
10. Add specificity-based advanced filesystem rules and expanded request-level
    network filters only after the simpler policy is stable.
11. Add Linux and Windows enforcement behind the same normalized profile model.

Unsupported selected behavior must fail closed at every stage.

## Open questions

1. Should the no-sandbox built-in be named `full_access` or
   `danger_full_access`?
2. Should `profile` replace `default_profile`, as recommended here, or should
   the longer name remain for explicitness?
3. Should URL path and HTTP-method filtering be in version 1, or should the
   first network slice accept only hosts, schemes, and ports?
4. Should exact listener ports be reserved when the profile is prepared or when
   the worker first starts?
5. Should a project file ever be permitted to request SSH, Docker, custom
   providers, or full access after a trust prompt, or should those settings be
   accepted only from user-level configuration?
6. Is one nested `run_on.inside` level sufficient for supported execution
   topologies?
7. Should advanced filesystem `rules` ship in version 1, or should the key be
   reserved until the simple root and denial model is implemented across all
   platforms?
8. Should package-source `worker_access` derive a network grant, or should all
   worker egress remain duplicated under `network.allow` for maximum visibility?

## Design influences

The format intentionally borrows concepts rather than exact syntax from:

- [OpenAI Codex permission profiles](https://developers.openai.com/codex/permissions),
  particularly the read-only, workspace, and unrestricted baselines,
  workspace-root scoping, filesystem specificity, domain policy, and Unix
  sockets;
- [OpenAI Codex sandboxing and approvals](https://developers.openai.com/codex/sandbox),
  for the distinction between constrained execution and explicit unrestricted
  execution;
- [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime),
  particularly explicit read denials, write allowlists, managed HTTP/SOCKS
  proxying, local sockets, and whole-process-tree enforcement;
- [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), treated as an
  execution and isolation provider rather than a second permission language;
- MCP Console's current [server-relay protocol](../docs/RELAY_PROTOCOL.md),
  which keeps the supervisor outside the worker boundary; and
- MCP Console's current [requirements design](../docs/REQUIREMENTS.md),
  especially the distinction between trusted dependency resolution and
  arbitrary worker code.

## Recommendation

Adopt the progressive profile design:

- `profile` is a built-in name, a named profile, or an inline mapping;
- common profiles remain complete and self-contained;
- scalar and sequence forms expand locally into mappings;
- one-parent inheritance and explicit list patches provide reuse without hidden
  merge behavior;
- `run_on` and `sandbox` remain separate, understandable axes;
- simple filesystem roots and explicit denials cover ordinary permission needs;
- advanced rules, reusable definitions, providers, proxy details, nested
  execution, and resource controls remain available without appearing in the
  starter file;
- every form normalizes into one explainable canonical configuration and fails
  closed when it cannot be enforced.

This keeps the first ten lines approachable while retaining the design space
needed for remote execution, containers, managed proxies, enterprise package
sources, alternate runtimes, resource controls, and third-party sandboxes.
