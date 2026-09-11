# MCP Console configuration

**Status:** Design sketch, not implemented configuration documentation. \
**Proposed file:** `.mcp-console/config.yaml` \
**Design date:** 2026-09-09

This document proposes a configuration language and its intended semantics.
It covers both near-term configuration and capabilities that will arrive through later pull requests.
Examples describe the proposed interface, not commands or options that can all be used in the current release.

The design has one central unit: **a profile is a complete session definition**.
It describes permissions, where the relay and worker run, how they are sandboxed, their language environments, resource limits, and storage settings.
Most configurations define that profile directly, without a `profiles` wrapper.
Reusable definitions are available when duplication becomes inconvenient.

## 1. Recommendation

Use YAML as the canonical format, with a small, strictly validated subset.
Keep permissions declarative and independent of the sandbox implementation.
Compose compute placement with permissions, but validate the resulting combination before starting anything.

Use these terms consistently:

| Term                     | Meaning                                                                                                      |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ |
| **Profile**              | A complete, selectable session definition.                                                                   |
| **Permissions**          | The filesystem, network, listener, and local-IPC access requested for the session.                           |
| **Target**               | Where the relay and worker run: transport, compute environment, and workspace.                               |
| **Transport**            | How the controller reaches the compute host: local or SSH initially.                                         |
| **Compute**              | The execution environment on that host: host process, Docker container, Docker Sandbox, or another provider. |
| **Sandbox provider**     | The implementation that enforces the requested permissions in that environment.                              |
| **Language environment** | The R installation and libraries, Python installation and environment, and SQL connections.                  |
| **Controller**           | The MCP server, which remains outside the worker sandbox.                                                    |
| **Launcher**             | The public `mcp-console sandbox` boundary and its provider adapters.                                         |
| **Runner / manager**     | The selected sandbox implementation and the component responsible for supervision and cleanup.               |

Do not use “environment” to mean all of a remote host, a Python virtual environment, and a permissions policy.
Do not require users to understand Codex internal structs or platform-specific sandbox flags.

The most consequential choices are:

- No configuration means `read_only`, with a private writable temporary directory and no worker-initiated network access.
- Project configuration requests capabilities; it does not authorize itself.
- Ordinary permission layers accumulate, and explicit denials win.
  Removing an inherited rule is an explicit operation, not an effect of list ordering.
- Unsupported restrictions fail before launch.
  There is no automatic unsandboxed or unrestricted-network fallback.
- A profile or target change replaces the worker generation.
  It does not mutate the security boundary around an existing R/Python session.

## 2. The common case

The proposed `mcp-console config init` writes this ten-line starter file:

```yaml
version: 1
extends: read_only
permissions:
  filesystem:
    allow_write: [.]
    deny_write: [.git, .agents, .codex]
    deny_read: [.env, ~/.ssh]
  network:
    mode: none
# Relative permission paths are rooted at the session workspace.
```

This starts from the built-in read-only policy, permits project edits, and explicitly lists the three requested write protections.
The two read denials are illustrative defaults in the generated file, not a claim that these are all possible secret locations.

`.` means the fixed session workspace, not whichever directory evaluated code has most recently selected with `setwd()` or `os.chdir()`.

The loaded configuration and its policy-bearing dependencies are additionally protected by the launcher.
They are not made writable just because `.` is writable.
See [Configuration authority](#5-configuration-authority).

For a project that needs only one API, replace the `network` block with:

```yaml
permissions:
  network:
    mode: allowlist
    allow: [https://api.example.com]
```

For a remote workspace, add:

```yaml
target:
  transport: {kind: ssh, host: research-box}
  workspace: /srv/projects/analysis
```

`research-box` is an OpenSSH host alias.
The permission paths now refer to the remote workspace and the remote user's home.
This does **not** upload the local project or select an unsandboxed remote execution path.

These examples are configuration fragments unless shown with `version: 1`.
They are meant to be merged into the surrounding file, not appended as duplicate YAML keys.

## 3. Relationship to existing systems

### Codex

Codex currently exposes built-in permission profiles named `:read-only`, `:workspace`, and `:danger-full-access`, as well as custom permission profiles with `extends`, filesystem rules, and network rules.
Its newer permission profiles are distinct from its older `sandbox_mode` settings.
This design takes those user-facing concepts without adopting two parallel permission models.[1]

MCP Console uses the names requested for this project:

| MCP Console       | Closest Codex permission profile | Older Codex sandbox mode |
| ----------------- | -------------------------------- | ------------------------ |
| `read_only`       | `:read-only`                     | `read-only`              |
| `workspace_write` | `:workspace`                     | `workspace-write`        |
| `full_access`     | `:danger-full-access`            | `danger-full-access`     |

These are conceptual mappings, not promises of identical baseline rules.

### Anthropic Sandbox Runtime

The supplied repository now redirects to `anthropics/sandbox-runtime`.
Its `allowWrite`, `denyWrite`, `denyRead`, domain rules, local binding, and Unix socket controls are useful vocabulary.
MCP Console uses snake_case equivalents and makes listener publishing a separate concern.
SRT also distinguishes filesystem and proxy-mediated network enforcement.[2]

Neither upstream's configuration is included verbatim or loaded implicitly.
Provider adapters translate one normalized MCP Console policy into their respective APIs.

### Current MCP Console

The inspected baseline is `main` at `c3027d71a86f837804ff3234dd1fab5c9103ff40`.
Its documented sandbox already allows host reads, restricts ordinary writes to private temporary storage, and denies direct networking.
Package preparation happens outside the worker sandbox, and `--no-sandbox` currently omits sandbox descendant cleanup.[3]

The proposed YAML loader, profiles, targets, and resource controls must not be presented as existing features.
The current CLI does not expose this proposed configuration interface.[4] This document also proposes some intentional behavior changes, identified below, rather than treating them as compatibility facts.

## 4. File shape, discovery, and profiles

### Top-level keys

A file has document-level keys and session-level keys:

| Document-level key | Meaning                                                                |
| ------------------ | ---------------------------------------------------------------------- |
| `version`          | Configuration schema major version. Initially `1`.                     |
| `default_profile`  | Profile selected when the caller supplies none; defaults to `default`. |
| `profiles`         | Optional map of named session profiles.                                |
| `definitions`      | Optional reusable `targets`, `permissions`, and `environments`.        |
| `server`           | Controller-wide settings, including admission and approval behavior.   |

All other recognized top-level keys form the implicit `default` profile:

| Session-level key | Meaning                                                               |
| ----------------- | --------------------------------------------------------------------- |
| `extends`         | One parent profile; defaults to `read_only` for the implicit profile. |
| `description`     | Human-readable purpose, not executable instructions.                  |
| `permissions`     | Filesystem, outbound network, listeners, and local IPC.               |
| `target`          | Transport, compute environment, and workspace.                        |
| `sandbox`         | Provider selection and provider-specific options.                     |
| `services`        | Named listeners and explicitly requested publication routes.          |
| `environments`    | R, Python, and SQL configuration.                                     |
| `packages`        | Preparation, resolution, repositories, and source policy.             |
| `resources`       | Per-session hard limits and scheduling preferences.                   |
| `env`             | Deliberately forwarded or assigned worker environment variables.      |
| `storage`         | Session records and cache routing/retention.                          |

Do not require a `session:` or `profiles.default:` wrapper for a one-profile configuration.
Do not allow the same implicit profile to be defined again as `profiles.default`.

### Discovery

The canonical project path is `.mcp-console/config.yaml`.
Support `.agents/mcp-console.yaml` as an alternative project path, not an additional automatic layer.
Finding both is an error that names the files and asks the user to select one with `--config` or consolidate them.

Choose the project root once at startup: an explicit workspace argument, then the discovered project configuration's parent project, then the invocation working directory.
Search upward only within the initial repository/workspace boundary; do not keep discovering new configuration after a worker changes directory.
Do not load a chain of ancestor project files implicitly.

A user-level configuration may live in the platform configuration directory, for example `~/.config/mcp-console/config.yaml`.
Managed administrator requirements are a separate trusted input, not a project override.

Recommended precedence for ordinary settings is built-in defaults, user-level configuration, the one selected project file, and explicit controller CLI settings.
Project settings still require authorization; precedence is not an authorization rule.
Administrator requirements are constraints on the result, not merely a lower-precedence configuration layer.

An explicitly selected `--config` replaces project discovery.
It does not bypass user policy, administrator requirements, or trust checks.

### Named profiles

```yaml
version: 1
extends: read_only
permissions:
  filesystem:
    allow_write: [.]
    deny_write: [.git, .agents, .codex]

profiles:
  review:
    extends: read_only

  api_work:
    extends: default
    permissions:
      network:
        mode: allowlist
        allow: [https://api.example.com]

  remote:
    extends: default
    target:
      transport: {kind: ssh, host: research-box}
      workspace: /srv/projects/analysis
```

`default`, the three built-in names, and any future names beginning with `:` are reserved.
User profiles cannot shadow them.
A named profile with omitted `extends` inherits `default`; examples should normally spell that out.

An explicit `extends` chooses the sole parent.
In particular, `review` above does **not** inherit the top-level project's write grant.
Otherwise a supposedly read-only review profile could accidentally remain writable.
Controller-wide settings and administrator requirements still apply to every selection.

Materialize only the selected inheritance chain, then apply missing engine defaults.
Reject unknown parents and cycles.
Do not support multiple profile inheritance or implicit cross-products of profiles.

Selecting a built-in directly produces its default local target, not the target from the implicit project profile.
The selection UI and approval must show this.
To remove sandboxing while retaining an already selected remote target, use the explicit policy-only override described under `full_access`, not an unrelated profile selection.

### Optional reusable definitions

```yaml
version: 1
extends: read_only

definitions:
  targets:
    lab:
      transport: {kind: ssh, host: research-box}
      workspace: /srv/projects/analysis
      compute: {kind: host}

  permissions:
    project_edits:
      filesystem:
        allow_write: [.]
        deny_write: [.git, .agents, .codex]

  environments:
    analysis:
      r:
        library:
          kind: managed
          requirements: [dplyr, ggplot2]
      python:
        environment:
          kind: managed
          requirements: [numpy, pandas]

profiles:
  remote_analysis:
    extends: default
    target: {use: lab}
    permissions: {use: project_edits}
    environments: {use: analysis}
```

`use` expands a definition from the matching namespace.
Sibling fields overlay the expanded value using the same merge rules as profile inheritance.
A component accepts one `use`, not a list.
Definitions may themselves use one parent definition; cycles are errors.

Definitions are conveniences, not additional authorization scopes.
Selecting a new combination of target and permissions must still be authorized as that combination.
Approval for a writable container is not approval for the same write policy on a production SSH host.

The three namespaces are enough initially.
Do not introduce separate registries for every small object, or force ordinary files to spread state across them.

## 5. Configuration authority

**A project file is a request, not a source of authority.** This matters even if MCP Console itself cannot write that file: another agent tool, a checkout, or a package script might change it.

On first use of a project configuration, the controller should show a normalized summary and obtain authorization through a trusted CLI or MCP-client approval channel.
When resolving target/provider identities requires network access or executable probes, first obtain limited authorization for those probe operations.
That authorization identifies the permitted endpoints, credentials, and probe executable identities; it does not authorize image preparation, package installation, or worker launch.
After resolving the target, executable, and image identities, obtain final authorization for the effective configuration bound to those identities before preparation or launch.
The trust record belongs outside the project, in controller-owned state.
It binds at least the project identity, selected profile's effective configuration, target identity, and relevant executable or image identities.

Loading or parsing an untrusted project file must not run its SSH command, Docker build, executable probe, package installer, or other configured program.
Authorization comes before side-effecting target preparation.

An agent may request a different authorized profile or propose configuration edits.
Neither action authorizes additional access.
If an agent is deliberately allowed to edit configuration, those edits still go through the same review.
There is no meaningful way to distinguish a safe policy change by whether a human or an agent happened to write its bytes.

Referencing a trusted user's profile or provider does not inherit its authority.
Attribute the complete resolved request, including overlays, to the actor selecting it.
A project cannot gain `full_access` merely by naming a user-defined profile; that selection still needs authorization.

Prefer approval of the effective configuration over approval of raw file formatting.
A change in comments need not invalidate approval.
A changed target, mount, provider, executable, environment forwarding rule, package source, manifest, service exposure, or policy dependency does.
Cached approval must also be invalidated when a mutable input changes what would actually execute.

An implementation may automatically accept a provably narrower permission change within an existing approved target and execution definition.
When the comparison is uncertain, ask again.
Do not attempt to infer that arbitrary Docker arguments or commands are “narrower.”

### Protected control inputs

In sandboxed profiles, protect the loaded configuration, loaded local configuration fragments, trusted policy state, launcher/provider definitions, and other files whose later privileged interpretation would grant authority.
Protect their identities and the relevant replacement operations, not only file-content writes.
Prevent bypass through a symlink, directory rename, or replacement of an ancestor entry.

`.mcp-console/` is not blanket read-only: it may contain requested writable cache and output directories.
Protect its control inputs specifically.
The starter explicitly denies writes to `.git`, `.agents`, and `.codex`, while the launcher also protects the configuration path itself.
Resolve Git worktree indirections when applying repository-metadata protections.

A full-access process does not have these worker-side protections.
Trust-store integrity against another unsandboxed process running as the same OS user is outside the promise of a worker sandbox.
Managed enforcement may require a separate identity or service.

### Snapshot before launch

Read configuration inputs into a bounded, validated snapshot, resolve the profile and target/provider identities under any required probe authorization, and obtain final authorization for that exact effective snapshot and its resolved identities.
Pass the resulting normalized policy to the launcher.
Do not approve a pathname and then have the sandbox reopen that mutable file later.

For local launches, use an inherited setup descriptor or an inline, non-secret serialized argument.
Keep target stdin/stdout/stderr as the target's streams.
For remote launches, use an authenticated setup channel or safely encoded and quoted bounded arguments.
Do not introduce a writable temporary policy file or recreate complicated setup-payload forwarding on the relay's stdin.

Referenced build files and manifests need an equivalent snapshot/content-hash boundary when they are subsequently used by a trusted builder.
Admit referenced configuration and manifest files before reading them: require bounded regular files in an authorized path namespace, not devices, FIFOs, or unbounded streams.
External file access needs approval; approval does not remove read/parser bounds.
Target-side reads require the corresponding authorized target operation.
This does not mean freezing all ordinary project source code for an interactive session.

### Approval settings

```yaml
server:
  approval_policy: on_request
```

`on_request` allows a trusted user approval interaction.
`never` means requests outside already authorized capabilities are denied, not automatically granted.
In noninteractive operation, an unapproved configuration is an actionable error.
Do not silently ignore the file and continue under a different profile.

Project configuration cannot set its own trust status, approve providers, replace managed requirements, or weaken controller approval behavior.
Such fields are trusted-user/administrator settings even if the same YAML schema represents them in a user-level file.

Managed requirements can constrain permitted profiles and targets, maximum writable roots, mandatory denials, network destinations, package sources, resource ceilings, and whether unsandboxed execution is permitted.
An incompatible request fails with its provenance; it is not silently clipped.

Configuration precedence and authority are separate.
Built-in and ordinary profile defaults are not mandatory ceilings; they remain extensible after authorization.
Only explicitly designated managed requirements impose non-widenable ceilings, whether supplied by a user, administrator, or organization.
A network ceiling includes the denial of destinations outside its allowlist, not just explicit deny entries.
Project approval cannot override it.
In particular, the built-in empty network allowlist must not make every later network allowance impossible.

## 6. Built-in profiles and merge semantics

### Built-ins

| Profile           | Filesystem                                                                                 | Worker outbound network     | Sandbox             |
| ----------------- | ------------------------------------------------------------------------------------------ | --------------------------- | ------------------- |
| `read_only`       | Read the visible target filesystem; write only private session temporary storage           | None                        | Required            |
| `workspace_write` | `read_only` plus workspace writes; protect `.git`, `.agents`, `.codex`, and control inputs | None                        | Required            |
| `full_access`     | No MCP Console filesystem restrictions                                                     | Unrestricted by MCP Console | Disabled explicitly |

Read-only is a mutation restriction, **not** a confidentiality guarantee.
Use read denials, `read: minimal`, a container, or a combination when host files must not be visible.
OS permissions and an outer host/container boundary always remain in force.

Private temporary storage is an explicit built-in exception.
Do not make all of shared `/tmp` writable.
Engine-owned logs and package preparation are separate from worker write permissions and must be reported separately.

The starter derives from `read_only` and explicitly adds workspace access to make its policy obvious.
Deriving from `workspace_write` is equally supported; repeating its protected paths is harmless and keeps the example self-explanatory.

### Ordinary values versus policy sets

For ordinary configuration, omitted values inherit, scalar values replace, mappings merge by key, and sequences replace.
Changing a tagged object's `kind` replaces that whole object rather than retaining incompatible fields from its old kind.
For example, changing a Python environment from `managed` to `existing` does not retain the managed requirements list.

Permission lists are different: `allow_read`, `allow_write`, `deny_read`, `deny_write`, network `allow`/`deny`, listener grants, and local-socket grants are sets that **accumulate** and deduplicate.
A plain `[]` adds nothing; it does not erase inherited protections.

To remove an inherited set member, use the explicit delta form:

```yaml
profiles:
  write_results_only:
    extends: default
    permissions:
      filesystem:
        allow_write:
          remove: [.]
          add: [results]
```

The same set field accepts either a sequence, meaning additions, or `{remove: [...], add: [...]}`.
Apply removals and then additions within a layer.
Unknown removals are errors, which catches misspellings.
Structured network rules are matched by their normalized complete value.
Do not add ordering-based `!pattern` negation or YAML-specific merge tags.

Removing an inherited denial is a potentially broader request and requires authorization.
Mandatory managed denials and launcher control-input protections cannot be removed by a project delta.
An explicit profile based on a different built-in is another way to avoid unwanted ordinary inherited grants.

### Permission evaluation

Filesystem grants are evaluated as sets, not as an ordered program:

```text
can_read(path) = matches(read_baseline OR allow_read OR allow_write)
                AND NOT matches(deny_read)

can_write(path) = matches(allow_write OR private_session_tmp)
                 AND NOT matches(deny_write OR deny_read)
```

Underlying OS/compute restrictions and administrator constraints also apply.
`allow_write` implies read access.
`deny_read` means no content access and also blocks writes to the denied location; a blind-write capability is not part of this format.
`deny_write` leaves permitted reads intact.

A denial wins even against a more specific allowance.
There is no automatic reopening of a child under a denied parent.
Use a less broad denial or explicitly remove the inherited rule.
This intentionally favors an easy-to-audit policy over a general exception language.

The absence of a grant is not an explicit denial.
Thus `read_only` can be extended with a write grant; an explicit `deny_write: ["."]` cannot be defeated by adding `allow_write: ["results"]`.

For network allowlists, any matching explicit deny wins, then at least one matching allow is required.
`mode: none` disables all outbound allows without needing to remove them; diagnostics show that they are inactive.

### Full access and no sandbox

The canonical configuration is:

```yaml
version: 1
extends: full_access
```

It may add a target, environments, resources, and storage.
It must not also claim enforced filesystem or network restrictions.
Such contradictory configurations are errors.
A hard resource limit can still be enforced by a separate supervisor or compute provider.

Also support `mcp-console serve --no-sandbox` as a deliberate policy-only override: retain the selected target and environment, disable the worker sandbox, and clearly report that configured worker permission rules are not enforced.
It remains subject to trusted approval and managed requirements.
This is an explicit user action, never an error-recovery path.

Internally `full_access` selects `sandbox.provider: none`.
Setting `provider:
none` alongside a restricted policy is otherwise rejected; do not silently reinterpret an apparently restricted YAML file as unrestricted execution.

**Proposed behavior change:** lifecycle supervision should be independent of security isolation.
Unsandboxed workers should still receive normal session ownership, process-tree cleanup where supported, and resource accounting.
The current no-sandbox behavior differs.[3] Expose any cleanup limitation in the launch report rather than implying isolation is needed to own a child process.

## 7. Filesystem rules and path meaning

```yaml
permissions:
  filesystem:
    read: all
    allow_read: []
    allow_write: [results, /scratch/analysis]
    deny_read: [~/.ssh, secrets, "**/.env", "**/.env.*"]
    deny_write: [.git, .agents, .codex, data/raw]
```

`read` is `all` or `minimal`.
`minimal` includes only the declared runtime's necessary executable/library/platform support paths; add project/data reads with `allow_read`.
The launcher must enumerate its runtime support grants in `config explain`.
It cannot silently override a user denial because it happens to need a file there; choose a different environment location or fail.

Literal paths name a file or an entire subtree.
No trailing `/**` is required for an ordinary directory.
This also covers descendants created after launch.

For glob rules, specify one portable grammar: `/` as the logical separator, `*` within one component, `?` for one nonseparator character, and `**` across components.
Dotfiles are included.
All other glob-like constructs are rejected initially; there is no brace expansion, regex, tilde-user lookup, or shell expansion.
Quote patterns in YAML.
A matched directory protects its subtree.

**Glob denials describe runtime policy, not just startup search results.** A provider that can only snapshot existing matches cannot claim to enforce a rule against future matching files or renames.
Reject that rule or require exact literal/subtree rules until it can be enforced.
Do not turn an implementation scan-depth limit into an undocumented hole in the configuration semantics.

Path namespaces are determined by the field:

| Field                                                     | Namespace and relative base                                                      |
| --------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `permissions.filesystem.*`                                | Final worker filesystem; relative to `target.workspace`                          |
| Language executable, library, project, and database paths | Final worker filesystem; relative to the workspace                               |
| Unix socket paths                                         | Final worker filesystem; normally absolute                                       |
| `target.workspace` with host compute                      | Destination host filesystem; must exist unless creation was explicitly requested |
| Docker mount `source`                                     | Compute host reached by the transport                                            |
| Docker mount `target`                                     | Container filesystem                                                             |
| SSH identity/configuration file                           | Controller filesystem                                                            |
| Dockerfile/build context read from the project            | Controller filesystem unless an explicit target-side source is selected          |
| `storage.logs.directory`                                  | Controller filesystem; relative to the controller's project root                 |
| Cache directories                                         | Target-side logical storage; relative to the worker workspace                    |

Support a small set of explicit path substitutions: `${project}` for the controller project root, `${workspace}` for the final worker workspace, and `${session_tmp}` for private target temporary storage.
`~` resolves in the field's own namespace.
Do not expand arbitrary environment variables in policy paths; an agent must not change a policy by changing `HOME` or `TMPDIR` later.

A controller `${project}` path is not automatically a valid remote mount source.
Reject cross-machine substitutions unless a declared staging operation provides the mapping.
Resolve physical cache backing paths in the launch plan, not through guesses made by the relay.

Normalization must account for symlinks, filesystem case rules, Windows drive and UNC roots, mount aliases, and not-yet-existing descendants.
A write grant to a directory must not authorize access to a symlink's unrelated target.
Read-denial guarantees are about objects reached through the enforced filesystem boundary; they are not a data-loss-prevention system that can erase previous copies, memory, or previously disclosed contents.

## 8. Outbound networking

### Three explicit modes

```yaml
permissions:
  network:
    mode: none
```

`none` means no worker-initiated outbound connections.
`allowlist` means only the requested destinations, through a managed and enforced mediation path.
`unrestricted` means no MCP Console outbound destination filtering, while filesystem sandboxing may remain enabled.

The mode defaults to `none`.
Adding an allow rule does not silently change it.
An inactive rule produces a diagnostic.
`unrestricted` with explicit filter rules is a validation error, rather than a configuration that appears filtered.

In `allowlist` mode the launcher starts the managed proxy automatically.
There is no separate feature flag whose omission leaves listed restrictions unenforced.
This intentionally differs from Codex's separately enabled proxy feature.[1]

### Destination allowances

```yaml
permissions:
  network:
    mode: allowlist
    allow:
      - https://api.example.com
      - https://*.data.example.org
      - protocol: tcp
        host: analytics-db.corp.example
        port: 5432
        allow_private: true
        addresses: [10.44.8.12/32]
    deny:
      - https://tracking.data.example.org
```

A string is an origin-shaped destination rule: a scheme, hostname pattern, and optional port, with no meaningful path or user information.
`https` supplies port 443 and `http` port 80.
A trailing `/` is allowed.
A bare hostname is rejected because its port scope would be ambiguous.

For host patterns, support exact names, `*.example.org` for subdomains but not the apex, and `**.example.org` for both.
Do not accept arbitrary regexes or substring matching.
Match complete normalized DNS labels; normalize case and a trailing dot.
IPv6 literals use brackets in origin strings.
A global `*` grant requires the explicit unrestricted mode instead.

An HTTPS destination grant authorizes an opaque connection to that host and port.
It is **not** a promise to restrict HTTP methods, URL paths, request bodies, or the application protocol sent over that connection.
The scheme shorthand must not disguise the limits of a non-intercepting CONNECT proxy.

A structured TCP rule requires a host and exact port.
`addresses`, when given, is an additional intersection with allowed destination CIDRs, not an alternate way to reach any host in those ranges.
Private, loopback, and link-local addresses are blocked by default; `allow_private: true` applies only to that rule.
Exact private-address allowances should be preferred for sensitive endpoints.
Do not turn on access to the entire LAN to reach one database.

Only TCP and HTTP(S)-style destinations are in the initial network grammar.
UDP, QUIC, and arbitrary raw sockets remain denied unless a later explicit rule type and capable provider support them.

### URL paths and methods

Full URL filtering is a stronger, separate capability:

```yaml
permissions:
  network:
    mode: allowlist
    proxy:
      tls_inspection: required
    allow:
      - url: https://data.example.org/public/**
        methods: [GET, HEAD]
```

A `url` rule with a path or `methods` requires HTTP-aware enforcement.
HTTPS requires an explicitly approved TLS-terminating proxy or equivalent trusted application gateway.
The example requests TLS inspection; it does not assume a CONNECT tunnel can see encrypted paths.
SRT's optional TLS termination is one possible upstream building block, not an assurance that every provider has this capability.[2]

Path rules match a defined normalized URL path, never a fragment.
The provider must specify percent-encoding and dot-segment normalization, reject ambiguous request-target forms, and authorize redirects independently.
Query and body filtering are not implied.
A path restriction is not a complete exfiltration control, even with `GET` only.

Do not mix an opaque whole-destination grant with path restrictions on that same destination when the opaque path would bypass them.
Reject an unenforceable combination.
Do not bypass inspection for a certificate-pinned or mTLS client while still reporting the URL restriction as effective.

### Enforcement and failure behavior

The sandbox must block direct bypasses, including raw sockets, alternate DNS, IPv6, UDP, and proxy-variable changes.
Proxy environment variables alone are not enforcement.
Validate the requested hostname and the actual resolved address together on each connection; handle DNS changes without turning a hostname allowance into arbitrary local-network access.

A non-proxy-aware database driver must work through an enforced endpoint route, a transparent compatible provider, or a managed TCP forward used by its connection adapter.
Merely setting `ALL_PROXY` is not enough.
When a provider cannot supply the required path, fail and explain the missing capability.
Preserve the intended server name and certificate verification if the logical endpoint is routed through a local forward.

Proxy failure closes existing mediation paths and blocks new access; it never opens direct access.
The manager owns proxy/tunnel lifetime and attribution.
Do not honor ambient `NO_PROXY` as an escape from policy.
Approved upstream corporate proxies may be used, but only behind the same destination enforcement.

Transport and preparation traffic are separate.
The controller can make the approved SSH connection even when the worker's network mode is `none`.
Image pulls and package downloads have their own authority and must appear separately in the effective configuration.

## 9. Local development: listeners, publishing, and IPC

These are three different capabilities: initiating an outbound connection, binding a listener, and connecting to a local IPC socket.
None should imply the others.

### A Shiny app reachable from another tool

```yaml
version: 1
extends: workspace_write
permissions:
  network:
    mode: none

services:
  shiny:
    protocol: http
    listen: {host: 127.0.0.1, port: 3838}
    publish: {scope: controller_loopback, port: auto}
```

A service declaration explicitly requests its exact listener and a managed publication route.
It is therefore a capability request, not just documentation.
The expanded policy lists the derived listener/forward grants and checks them against managed requirements.
Users do not have to repeat those grants in a second list.
It does not grant arbitrary outbound loopback access.

The worker can run:

```r
shiny::runApp(".", host = "127.0.0.1", port = 3838, launch.browser = FALSE)
```

`runApp` accepts a bind address and TCP port.[5] The app may keep its cell active while the browser interacts with it; polling/interrupts must remain available.
The configured environment must contain Shiny or permit its explicit preparation.
The service does not implicitly install or run application code.

The manager establishes a forward from a controller loopback port to this specific worker service.
On SSH, that includes an authenticated channel to a target-side connector; in a container or isolated network namespace, it includes the necessary namespace-aware connector.
Publishing a container port that cannot reach a loopback-only app is not an acceptable implementation.

Return the reachable URL, service name, session/generation, and access scope in session metadata.
A browser tool running on the controller host can use that URL.
A cloud browser cannot reach the user's loopback address merely because a port is open: it needs a separately approved authenticated tunnel/exporter.
Reserve `scope: authenticated_tunnel` for such a provider, with explicit recipient/authentication and lifetime.
Never default to a public URL or a non-loopback bind to make an otherwise unreachable browser work.

HTTP publishing must support WebSocket upgrades and long-lived connections.
Forward only the named service, not an unrestricted tunnel into the sandbox's network namespace.
Attribute listener ownership to this worker generation; a port conflict must not publish another process's service.
Close the forward and retire the service's descendants when the session ends or is replaced.

`mode: none` still blocks new outbound connections.
Responses on the approved inbound service are allowed and can carry data; service publication is itself a disclosure capability, not “zero possible data transfer.”

### A listener without publication

```yaml
permissions:
  network:
    listen:
      - {protocol: tcp, host: 127.0.0.1, port: 8000}
```

This permits the bind only.
It neither publishes the port nor authorizes connections to unrelated host services.
Non-loopback binding must be explicit and separately authorized.

Automatic allocation is safe when the manager owns the listening socket, as for `publish.port: auto`.
For an application-owned listener, the application must bind port zero and report its bound endpoint, or accept a supported socket handoff.
Never probe a free port, release it, and promise it remains reserved.

### Unix sockets and named pipes

```yaml
permissions:
  network:
    local_sockets:
      allow:
        - {kind: unix, path: /run/user/1000/example.sock}
      deny: []
```

A future Windows adapter can use `kind: named_pipe` with an exact pipe name.
Connecting and binding/creating an IPC endpoint should be separate operations; the `allow` entries above permit connecting only.
Filesystem visibility of a socket does not itself authorize connection.

Do not include the Docker daemon socket or SSH agent socket in defaults.
Authorizing an IPC endpoint authorizes the service behind it; it is not merely file access.
A Docker API socket can defeat a container's intended host boundary.
Such grants need explicit high-risk approval and must not be described as narrow just because they name one filesystem path.

## 10. Targets: local, SSH, Docker, and other compute

### Local host

```yaml
target:
  transport: {kind: local}
  compute: {kind: host}
  workspace: ${project}
sandbox:
  provider: auto
```

This is the ordinary default.
`auto` selects an available provider that can honor the complete policy.
It does not mean “try something, then run without a sandbox.” Report the selected provider and effective capabilities before launch.

### SSH host

```yaml
target:
  transport:
    kind: ssh
    host: research-box
    executable: ssh
    setup:
      install: never
      executable: /opt/mcp-console/bin/mcp-console
    disconnect:
      action: terminate
      lease: 30s
  compute: {kind: host}
  workspace: /srv/projects/analysis
sandbox:
  provider: auto
```

The minimal form in section 2 inherits these conservative behaviors: use the host's OpenSSH configuration, require an existing compatible target executable, and do not detach work by default.
Support explicit `user`, `port`, and `identity_file` overrides, with the latter interpreted on the controller.
Perform normal host-key verification.
Do not disable it or forward the SSH agent as an implementation convenience.

A future `setup.install: if_missing` may stage a version-matched, integrity- verified executable and runtime assets after approval.
It is not an unpinned `curl | sh`.
Pin/check the protocol and runtime compatibility before launching the relay.
Binary download, staging, and package preparation are visible setup operations with their own network authority.

The controller launches the remote manager through the public launcher boundary; the manager applies the remote sandbox and then starts relay and worker.
The server is not taught the details of a remote Seatbelt, bubblewrap, Windows, or third-party sandbox implementation.

No PTY by default.
Relay stdout remains protocol data, not shell banners or setup logs.
The manager uses ownership/lease semantics to retire the remote process tree after disconnect.
A local PID ownership flag alone cannot enforce remote cleanup after a network partition.

The remote host is trusted with submitted code, data, and any explicitly forwarded credentials.
A sandbox on that host does not protect the session from its administrator.
Remote ambient MCP Console configuration must not broaden the controller-authorized policy; trusted target-local requirements may further restrict it and must be disclosed.

There is no implicit project synchronization.
Optional later synchronization must specify source, destination, direction, deletion/conflict policy, excluded control/secret files, and authorization for writeback.
An existing remote workspace is sufficient for the first SSH implementation.

### Docker container

```yaml
target:
  transport: {kind: local}
  workspace: /workspace
  compute:
    kind: docker
    image: company/mcp-console:analysis
    pull: if_missing
    mounts:
      - source: ${project}
        target: /workspace
        access: read_write
sandbox:
  provider: auto
```

The mount exposes a path to the compute environment; `access: read_write` is an upper bound, not a replacement for the session filesystem policy.
A read-only profile still requires read-only effective access.
A workspace-write profile still protects its denied subpaths.
Fail if the selected enforcement stack cannot honor that combination.

Pin the resolved image digest in the approved launch plan.
Pull/build policy should distinguish `never`, `if_missing`, and an explicit refresh; do not silently move a running profile to a different mutable tag.
Images must contain or receive compatible runtime components through the approved setup path.

Use typed fields for image, mounts, user, working directory, devices/GPU access, resource limits, and lifecycle.
Do not default to privileged mode, a host PID or network namespace, a daemon socket mount, or unnecessary capabilities.
A container is compute placement and isolation, not automatically an exact implementation of all requested permissions.

`extra_args` may exist as a trusted escape hatch, but arguments conflicting with normalized mounts, networking, lifecycle, or limits must be rejected for a restricted profile.
Do not silently let raw provider flags override the policy.
An arbitrary container wrapper belongs under the explicit command-provider contract when it cannot be validated structurally.

### Docker on an SSH host

The two dimensions compose without another top-level target kind:

```yaml
target:
  transport: {kind: ssh, host: research-box}
  workspace: /workspace
  compute:
    kind: docker
    image: company/mcp-console:analysis
    mounts:
      - source: /srv/projects/analysis
        target: /workspace
        access: read_write
sandbox:
  provider: auto
```

The mount source is on `research-box`, not on the controller.
Filesystem permissions refer to `/workspace` inside the container.
Cache backing and service forwarding are likewise resolved across both boundaries.

### Docker Sandbox

```yaml
target:
  transport: {kind: local}
  workspace: /workspace
  compute:
    kind: docker_sandbox
    template: company/mcp-console-sandbox:analysis
    lifetime: session
sandbox:
  provider: compute
```

Use a distinct compute kind.
Docker's current Sandboxes documentation describes a microVM-backed execution environment, not merely a synonym for an ordinary Docker container.[6] MCP Console should hide the particular vendor CLI spelling behind its adapter instead of incorporating changing command names into the common schema.

`provider: compute` asks that provider to enforce the normalized permissions.
If it cannot enforce an exact read denial, network rule, or other requested restriction, it fails or requires an explicit compatible inner provider.
It does not report a broad outer boundary as if the narrower policy were enforced.
A workspace mapping must be specified or supplied by an explicit adapter default that maps the project only and appears in the launch plan.

### Embedded or separate Dockerfile

```yaml
target:
  compute:
    kind: docker
    build:
      context: .
      dockerfile: .mcp-console/Dockerfile
```

For very small definitions, permit an alternative:

```yaml
target:
  compute:
    kind: docker
    build:
      context: .
      dockerfile_inline: |
        FROM company/mcp-console-base:approved
        ENV LANG=C.UTF-8
```

`dockerfile` and `dockerfile_inline` are mutually exclusive.
Prefer the separate file once the definition grows beyond a few lines; it gets normal Dockerfile editing, review, and linting.
Both remain under `.mcp-console/` when desired.

The controller must authorize/snapshot the build inputs, bound the context, respect exclusions, and avoid uploading secrets or excluded control files.
Remote builds require explicit context staging.
Build-time network and secret access are separate from runtime permissions.
Image builds are code execution, not inert configuration parsing.

## 11. Sandbox and command-provider contract

`sandbox.provider` is one of `auto`, `native`, `srt`, `compute`, `command`, or `none`.
`native` means the bundled MCP Console native runner; it does not expose its upstream Codex patch details.
`srt` identifies an adapter, not automatic loading of the user's unrelated SRT settings.

A third-party sandbox can be configured without putting shell text in the core schema:

```yaml
sandbox:
  provider: command
  command:
    executable: /usr/local/bin/company-sandbox-adapter
    args: [run, --region, us-east, --setup-fd, "${setup_fd}"]
    env:
      COMPANY_SANDBOX_TOKEN: {from_env: COMPANY_SANDBOX_TOKEN}
    protocol: mcp-console-launch-v1
```

This adapter is trusted executable configuration.
It receives the normalized policy, target argv, approved environment, resource requirements, and lifecycle parameters through a versioned setup contract.
`${setup_fd}` is an injected placeholder, not general template evaluation.
The adapter launches the exact provided argv without reconstructing it through a shell.

An adapter that places the worker in another compute environment can use the same `command` object under `target.compute`, with `kind: command`.
A policy-enforcing compute adapter can be selected with `sandbox.provider:
compute`.
Keep its placement role distinct from its permission enforcement role even if one executable performs both.

The contract requires separate setup/control and target data paths; relay stdout is not mixed with capability reports.
It must support cancellation, exit status, signal/interrupt forwarding, environment/path mapping, descendant retirement, and the applicable resource/network/service controls.
Capability negotiation takes place before execution, not after a partially unconfined worker has started.

For simple existing wrappers, a command passthrough form may accept an argv placeholder and report **externally managed** enforcement.
It must not claim MCP Console's normalized policy is enforced just because a command exits successfully.
Supporting restricted profiles requires a trusted policy-aware adapter or a separately enforced inner sandbox.
Provider self-reporting is a compatibility check, not proof that an untrusted executable is safe.

The public server-to-launcher architecture remains:

```text
controller <-> mcp-console sandbox <-> [transport/compute/manager] <-> relay <-> worker
```

The launcher subsystem owns provider selection and proxy setup through its adapters.
Sandbox/provider process-tree supervision and cleanup belong to the selected runner or manager.
The server retains logical relay lifetime orchestration and retirement; the relay retains direct-worker signal delivery, bounded termination, and reaping.
The relay remains unaware of sandbox implementation.
Keep the normalized policy and adapter contract independent of upstream Codex internals so a rolling upstream patch set can remain small and isolated.

## 12. R and Python environments

Package availability, selecting an interpreter, preparing an environment, automatic missing-package resolution, and importing a package are separate operations.
Configuration should not collapse them into one `requirements` list with implicit side effects.

### Managed defaults with always-available packages

```yaml
environments:
  r:
    executable: auto
    library:
      kind: managed
      lifetime: session
      requirements: [dplyr, ggplot2, shiny]
  python:
    executable: auto
    environment:
      kind: managed
      lifetime: session
      requirements: [numpy>=2, pandas, matplotlib]
```

`managed` means MCP Console prepares and selects an environment from the declared requirements.
The R implementation may use an `ir` library; Python may use a managed virtual environment.
These packages are available, not attached, imported, or executed automatically by configuration.

A session-lifetime environment can reuse immutable cached artifacts.
It need not redownload everything at each start.
Worker mutation of shared installed packages is not permitted; dynamic additions produce an approved managed environment update or overlay, rather than editing a user's unrelated library.

Engine-required bridge/runtime packages are distinct from user packages and are reported explicitly.
A user list does not remove runtime components necessary for the chosen language configuration.
Conversely, a Python-only future runtime must not require R just because a historical default did.

A release may supply a documented built-in default manifest.
Its exact contents belong in implementation documentation and resolved-environment metadata, not in the permanent meaning of `version: 1`.
Preserve a lock or explicit manifest when reproducibility across MCP Console releases matters.

Inline requirements are enough for small files.
A typed `requirements_file` keeps a larger manifest beside the configuration:

```yaml
environments:
  r:
    library:
      kind: managed
      requirements_file:
        format: mcp_console
        path: .mcp-console/requirements.yaml
  python:
    environment:
      kind: managed
      requirements_file:
        format: mcp_console
        path: .mcp-console/requirements.yaml
```

The referenced `requirements.yaml` uses a separate, data-only manifest schema:

```yaml
version: 1
r: [dplyr, ggplot2]
python: [numpy>=2, pandas]
```

Each language consumes its own list, and the complete manifest is strictly validated.
Inline `requirements` and `requirements_file` are mutually exclusive within a language environment.
Manifest paths refer to the target workspace; read and snapshot them through the authorized target path, not accidentally from the controller's similarly named directory.

Additional explicit formats can include `requirements_txt` for Python and a supported data-only `ir` manifest format for R.
An adapter must reject formats it does not understand.
Do not evaluate an arbitrary R or Python script merely to discover requirements.
These manifests declare requirements; use a supported lockfile/project environment when an exact resolved dependency graph is needed.

### Existing installations and environments

```yaml
packages:
  resolution: disabled

environments:
  r:
    executable: /opt/R/4.5/bin/R
    library:
      kind: existing
      paths: [/opt/company/R-library]
  python:
    executable: /opt/company/python/bin/python3
    environment:
      kind: existing
      path: /opt/company/python
```

`existing` never installs into or modifies the selected environment.
Missing packages produce ordinary actionable errors.
An explicit interpreter must be compatible with its selected library/environment; conflicting selectors are an error, not competing priorities.

A future managed installation selector can use `version` instead of `executable`.
`version` does not silently download an interpreter unless preparation policy authorizes that action.
Interpreter downloads are not package index access and need their own approved source.

Typed configuration takes precedence over ambient `R_HOME`, `RETICULATE_PYTHON`, and virtual-environment hints.
Conflicts must be reported; do not depend on accidental environment-variable ordering.
The selected executables and libraries are target paths, not controller paths.

### Explicit project environments

```yaml
packages:
  resolution: explicit
  frozen: true

environments:
  r:
    library:
      kind: project
      manager: renv
      path: .
      lockfile: renv.lock
      mutation: never
  python:
    environment:
      kind: project
      manager: uv
      path: .
      lockfile: uv.lock
      mutation: never
```

`mutation: never` means use an existing project environment without changing its contents or lock.
A requested preparation that would modify it fails with instructions.
`mutation: overlay` may prepare a separate managed environment from the locked project plus approved additions, leaving the project unchanged.
`mutation: project` is an explicit request to prepare/update the project environment and requires the corresponding authority.

Explicit project selection wins over discovery.
An optional `kind: auto` can perform documented discovery on the target; it must report the selected manager and reject ambiguous competing environments.
Do not activate arbitrary project startup code on the trusted controller while detecting an environment.
R startup files and Python startup hooks execute only within their appropriate approved execution boundary.

Do not implement “always available” Python packages by stacking unrelated virtual environments on `PYTHONPATH`.
Resolve the project and required additions as one environment, or reject incompatible requirements.
Likewise, R library composition must respect the chosen R installation, ABI, precedence, and loaded-package constraints.

Changes to interpreter, target, or environment kind require a new worker.
Additive package updates may preserve live state only where the runtime's existing activation contract supports that safely.
A failed preparation must not half-commit a new environment or replay user code automatically.
Current MCP Console already distinguishes preparation from import/attachment and has explicit retained-environment transitions.[7]

## 13. Package resolution, mirrors, and preparation authority

### Resolution modes

```yaml
packages:
  resolution: explicit
  offline: false
  frozen: false
```

| Mode        | Behavior                                                                                                         |
| ----------- | ---------------------------------------------------------------------------------------------------------------- |
| `automatic` | Prepare declared requirements, accept explicit additions, and permit supported missing-package inference.        |
| `explicit`  | Prepare declared requirements and explicitly requested additions; missing imports alone do not install anything. |
| `disabled`  | Do not invoke a package resolver, installer, or downloader. Selected environments must already exist.            |

`offline: true` blocks preparation network access while allowing use of existing artifacts and cache-only resolution.
`frozen: true` requires an applicable locked solution and prevents changing it.
`disabled` is stronger than offline: a local build or cache-only install is still an installation.

These controls cover MCP Console's resolver path, including R, Python, and DuckDB extension preparation.
They cannot stop arbitrary evaluated code from implementing an installer if its ordinary filesystem and network permissions allow that.
Runtime enforcement remains the sandbox's responsibility.

**Recommendation:** make `explicit` the new configuration-era default.
This is an intentional change from current automatic missing-package resolution.[7] Keeping `automatic` as a compatibility default is a reasonable alternative; choose that independently from the filesystem profile, and document that a read-only worker can still request separately authorized preparation.

For the first configuration implementation, make the existing preparation trust visible rather than implying a new boundary: the compatibility preparation default is `network: sources` and `builds: trusted_host`.
This is separate from the recommendation to use `resolution: explicit`.
Host preparation requires approval of that authority; an approved worker network rule is not enough.
Use `builds: isolated` for untrusted source builds once supported, or `builds: disabled` to require suitable prebuilt artifacts.
An eventual change to isolated preparation by default is a separate migration, not something the YAML loader alone delivers.

### Corporate sources

```yaml
packages:
  resolution: explicit
  sources:
    r:
      repositories:
        CRAN: https://packages.corp.example/cran/approved
    python:
      indexes:
        - name: approved
          url: https://packages.corp.example/pypi/simple
      index_strategy: first_index
    duckdb:
      repositories:
        - https://packages.corp.example/duckdb
  source_policy:
    allowed_kinds: [registry]
  preparation:
    network: sources
    builds: isolated
```

An explicitly configured repository/index collection **replaces** that language's implicit public defaults.
Do not append public PyPI or CRAN as a fallback.
Python index ordering is significant; `first_index` selects the first index containing a package, rather than choosing competing versions across all indexes.
This follows the safety motivation documented by uv.[8]

Mirror preference is not complete package approval.
Enforce `source_policy` on all direct and transitive requirements and build dependencies, checking source kinds as well as approved repositories.
Project package-manager configuration and ambient index/repository variables cannot silently add a source outside this policy.
A corporate configuration may require different approved copies of built-in runtime dependencies; missing approved artifacts are errors, not permission to bypass the mirror.

With `builds: trusted_host`, retain the current accepted inputs: documented trusted `ir` references with `IR_NO_LOCAL_SOURCES=1`, named Python PEP 508 registry requirements, and validated DuckDB extension names.
`source_policy` may further restrict those inputs; it cannot authorize local sources, Python URL/VCS direct references, or arbitrary DuckDB source selectors on this path.
Supporting additional source kinds requires a separately implemented isolated preparation boundary and explicit approval of that expanded authority; reject them until both exist.

`network: sources` gives the trusted preparation path access only to the resolved approved repository/artifact destinations.
Repository redirects and separate artifact/CDN hosts must be explicitly admitted or supplied by trusted repository metadata under a defined policy; an arbitrary redirect is not an automatic grant.
Proxy credentials and repository authentication are held by the preparation path, not automatically exposed to the worker.

There are three distinct controls: which packages can be requested, where their artifacts may come from, and what installation/build code can do.
A registry allowlist alone does not make package installation safe.
Approval must bind a requirement to its permitted ordered sources and fallback behavior, not accept the cross-product of independently approved package names and registries.
Apply this to declared, transitive, and runtime-inferred requirements before installation or cache activation.

`builds: isolated` requests isolated preparation with a bounded source snapshot, controlled network, resource limits, and owned output directories.
If unavailable, fail when a build is needed.
`builds: trusted_host` names the compatibility behavior with a stronger trust requirement; `builds: disabled` permits only suitable prebuilt artifacts.
Do not present current host-side preparation as isolated until that boundary exists.

Preparation occurs for the target's platform and runtime.
On SSH this normally means a target-side trusted preparation process; for containers it means a compatible builder or image layer.
Never copy the controller's arbitrary site-packages or compiled R library into an incompatible target.

Preparation remains controller-owned even when it runs remotely.
Use a separate preparation launch rather than assigning installer lifecycle to the worker relay.
A command adapter supporting only relay launch must use pre-existing environments until it also supports the preparation lifecycle.
Requests that need unsupported preparation fail.

Cache keys include the effective manifest/lock, selected runtime and ABI, target platform/architecture, source policy, and artifact integrity information.
Do not reuse artifacts from an unapproved public source simply because the name and version match a corporate package.

## 14. SQL installations and connections

SQL is a map of named connections with an explicit default:

```yaml
environments:
  sql:
    default: scratch
    connections:
      scratch:
        driver: duckdb
        database: ":memory:"
        access: read_write
      archive:
        driver: duckdb
        database: data/archive.duckdb
        access: read_only
        options:
          threads: 2
```

The default with no SQL configuration is one DuckDB in-memory connection.
Its read-write database semantics do not grant filesystem writes.
The in-memory connection belongs to a worker generation and is lost when that generation is replaced.

`driver` is a tagged adapter name, initially `duckdb`, later `sqlite`, `postgres`, or an explicitly installed adapter.
`options` is adapter-specific, validated by that adapter, and cannot override reserved identity or access fields.
Unknown drivers and options are not silently ignored.

For a persistent writable database:

```yaml
permissions:
  filesystem:
    allow_write: [state/database]
environments:
  sql:
    default: main
    connections:
      main:
        driver: duckdb
        database: state/database/analysis.duckdb
        access: read_write
```

A dedicated directory makes the necessary database and sidecar/temp-file scope clear.
A connection declaration never grants broad parent-directory write access implicitly.
Read-only archives should also have filesystem write denials when another grant would otherwise let the worker reopen or replace them.

For a future PostgreSQL adapter:

```yaml
permissions:
  network:
    mode: allowlist
    allow:
      - protocol: tcp
        host: analytics-db.corp.example
        port: 5432
        allow_private: true

environments:
  sql:
    default: analytics
    connections:
      analytics:
        driver: postgres
        host: analytics-db.corp.example
        port: 5432
        database: analytics
        user: agent_reader
        password: {from_env: ANALYTICS_DB_PASSWORD}
        access: read_only
        tls: verify_full
```

Connection selection and network authorization remain separate.
Validation checks that the requested endpoint has a usable allowed route and provides a focused diagnostic rather than broadening network access.
Configuring a default connection does not prohibit user code from making other connections that its permissions allow.

`access: read_only` asks the adapter to open a read-only connection; it is not an independent security boundary against arbitrary R/Python/SQL.
Use database-side roles for a remote service and filesystem restrictions for an embedded database.
DuckDB exposes external-access/security controls separately from connection access mode.[9] Extension loading, remote reads, exports, and other external I/O still need their corresponding preparation and sandbox permissions.

For installations, allow a SQL connection's `runtime` selector to name an embedded adapter/version, an approved driver/library, or a supported CLI executable.
Do not require a fake “SQL interpreter” common to all databases.
The usual DuckDB adapter uses the selected language environment; a PostgreSQL connection does not need a local PostgreSQL server installation.

For example, a future SQLite adapter could explicitly select a CLI installation:

```yaml
environments:
  sql:
    default: archive
    connections:
      archive:
        driver: sqlite
        runtime:
          kind: cli
          executable: /opt/sqlite/bin/sqlite3
        database: data/archive.sqlite
        access: read_only
```

Only an adapter that actually implements this CLI interaction can accept that selector.
A library-backed adapter can instead define `runtime.kind: embedded` with a supported `version` selector.
Runtime selection does not imply installing anything, spawning a database server, or obtaining network/filesystem grants.

Concurrency must honor each driver's locking model.
Several profiles selecting the same writable database path must not be treated as independent safe writers.
Admission may reject conflicting use or require an explicit supported access mode.
A driver-specific limitation is an actionable error, not a reason to silently switch to another database.

## 15. Resources and scheduling

```yaml
server:
  limits:
    max_sessions: 2

resources:
  memory: 8GiB
  cpus: 2
  processes: 128
  wall_time: 2h
  priority: background
```

Hard resource limits apply to the aggregate worker/relay process tree, not just the initial PID.
`memory` is a memory limit, `cpus` is CPU-time bandwidth in core equivalents, and `processes` limits process creation.
`cpus: 2` means at most two cores' worth of CPU time over the provider's quota interval, not “use CPU numbers 0 and 1” and not “2% of the machine.” Avoid an ambiguous `max_cpu_percent` setting.

`wall_time` is a session lifetime budget, not an MCP polling timeout.
Optional future per-evaluation limits must have a separate name.
Preparation processes need their own budget and admission accounting; do not imply a worker memory limit contains an unsupervised host package build.

`priority: background` is a portable scheduling preference, not a hard CPU limit.
Also permit a target-specific `nice: 10` override where supported.
Reject specifying both when their mapping conflicts.
An unsupported scheduling hint may produce a visible warning; an unsupported hard resource limit is a launch error.
Do not relabel an unenforced limit as a hint.

Recommended scheduling default is `background`, to reduce competition with the user's other work.
Leave memory/CPU hard caps unset until requested or imposed by managed policy; arbitrary built-in caps can make ordinary analysis fail.

`server.limits.max_sessions` is the number of active sessions admitted by this controller, not a fleet-wide host limit.
Reserve capacity atomically before preparation/start, count starting/running/retiring generations, and release it after cleanup.
Idle-but-live sessions count.
Reject excess requests with an explicit capacity error rather than creating an unbounded hidden queue.

This remains useful before concurrency exists: the supported value is initially `1`; selecting `2` must fail as unsupported until implemented.
Multiple separate MCP Console servers require a shared supervisor or external resource manager for a real aggregate host ceiling.
Do not claim this per-controller setting provides one.

## 16. Environment variables and secrets

```yaml
env:
  inherit: [LANG, LC_ALL, TZ]
  set:
    OMP_NUM_THREADS: "2"
    PROJECT_TOKEN: {from_env: PROJECT_TOKEN}
  unset: [SSH_AUTH_SOCK]
```

Use a controlled baseline needed to launch the selected runtimes, not automatic inheritance of every controller environment variable.
`inherit` names variables from the controller's approved environment; values are deliberately forwarded to the target.
`from_env` is an explicit credential/value reference evaluated on the controller unless a later secret-provider type says otherwise.

Resolved secret values go through protected setup channels, not command-line arguments or routine configuration dumps.
`config explain` shows the reference and recipient, not the secret.
A secret intentionally given to the worker is readable by arbitrary code in that worker; do not imply stronger isolation.
Database credentials are delivered only when the connection is prepared, and provider/SSH/package-proxy credentials stay out of the worker unless explicitly requested.

Environment values are not a route to change managed cache destinations, interpreter choice, proxy enforcement, or library paths behind the typed schema.
Reject conflicting assignments to fields controlled by typed configuration.
Provider-specific environment variables belong to the provider's `command.env`, not the worker `env` map.

Worker `env` values belong only to the worker and its descendants, not the controller, SSH/Docker launcher, provider, relay, or resolver.
In sandboxed sessions, inject them after enforcement is established.
Helpers use an implementation-owned baseline plus separately approved helper configuration.
A worker `LD_PRELOAD` assignment must not affect an unsandboxed launcher.
Provider `command.env`, executable, argv, and working directory are authority-bearing configuration: a project overlay requires authorization even when the underlying provider is user-defined.
A restricted profile fails if its provider cannot keep helper and worker environments separate.

Prefer secret references to inline secrets.
Do not add a general-purpose `$(command)` interpolation or executable secret lookup in untrusted project configuration.
A future secret provider is another explicitly authorized adapter, not shell expansion.

## 17. Logs, caches, and retention

### Logs and session records

```yaml
storage:
  logs:
    directory: .mcp-console/logs
    retention:
      max_age: 30d
      max_size: 2GiB
      sweep_interval: 24h
```

This directory is on the controller.
Session records from remote workers are still collected by the controller; target-manager diagnostics can be forwarded or kept in a separately disclosed target spool.
The default without this block is the platform's MCP Console state directory, not an unbounded project folder.

Store records under project/profile/session/generation identities.
Retention operates on complete retired record sets, including referenced image artifacts, not arbitrary files selected by a glob.
`max_age` is time since session closure; `max_size` evicts oldest closed record sets.
Never delete a live journal to meet a retention target.
Report when pinned/live records prevent reaching the budget.

Sweep no more often than `sweep_interval` while the controller runs, including an overdue sweep at startup.
This is not a promise of a background daemon while MCP Console is stopped.
Coordinate cleaners with locks.
A user-invoked prune command may force an eligible sweep and show a dry run.

Logs may contain code, input, output, and secrets printed by the user program.
Known-secret redaction can be offered as best effort, not a confidentiality guarantee.
Protect controller records from worker mutation; a requested log location does not grant the worker write access to it.
Do not execute an exported transcript as part of recording or cleanup.

### Granular cache routing

```yaml
storage:
  cache:
    directory: default
    cleanup:
      interval: 7d
      unused_for: 30d
      max_size: 10GiB
    r:
      directory: .mcp-console/cache/r
      tools:
        ir: inherit
        pak: inherit
        renv: inherit
    python:
      directory: default
      cleanup: disabled
    duckdb:
      directory: default
      cleanup: disabled
```

The R family is placed under the project's `.mcp-console/cache/r`; Python and DuckDB retain their target-side native locations.
These are cache locations, not selections of an R library, Python interpreter, or persistent SQL database.
A database file is durable data and is never swept as a cache.

`directory: default` means use the tool's native target-side location without an override.
Omitted per-language settings inherit the configured family/root policy.
`inherit` on a known tool routes it to a tool-specific subdirectory of the R family root.
A tool may instead have an explicit directory.
Unknown tool names are errors, not promises that arbitrary packages respect a universal cache variable.

Routing is implemented through tested per-tool adapters.
Examples include `R_USER_CACHE_DIR` for cooperating R packages, `RENV_PATHS_ROOT`/cache settings for renv, `R_PKG_CACHE_DIR` for pkgcache-backed tooling, and `UV_CACHE_DIR` for uv.[10][11][12][13] The `ir` adapter must use its supported cache controls, not an invented environment variable.
DuckDB's extension directory is an adapter-controlled setting, separate from its temporary spill directory.[9]

Do not globally change `XDG_CACHE_HOME` just to move R caches; that would also move other tools' caches and violate the example's intent.
Apply mappings consistently to target-side preparation and worker runtimes, with explicit container mounts/backing storage when their namespaces differ.

No configuration can force every arbitrary R package to honor conventional cache settings.
Guarantee routing for supported adapters and enumerate them in `config explain`.
Filesystem policy determines what happens when other code tries another location.

### Ownership and cleanup safety

Only prune MCP Console-owned cache entries, or entries deleted through an explicitly opted-in native tool cache API.
A path such as a user's default uv cache is not a license to recursively remove it.
Natural shared caches are preserved by default; `cleanup: disabled` makes that intent explicit above.

The generic cleanup block applies to owned namespaces only.
Cache age means last use, not file modification time.
Pin active environments and their artifacts with leases/reference counts so cleanup cannot break a live worker.
Active-use protection spans every process sharing the cache, not just this controller.
Coordinate activation, publication, and cleanup across processes; if that cannot be done safely, skip shared-entry cleanup or use a private cache.
Respect tool locks and use supported pruning APIs.
uv specifically documents its cache-management commands and concurrency expectations.[14]

Reject dangerous cleanup roots, symlink traversal out of owned roots, and parent directories that contain durable user data.
Moving a cache creates a new location; it does not silently migrate or delete the old one.
Unused owned entries may be removed when due or to satisfy `max_size`; never indiscriminately clear every cache on a timer.

Separate mutable worker application caches from trusted installer/download and immutable environment caches.
A writable project cache must not let worker code poison a shared executable/package cache later consumed with controller permissions.
Likewise, downloaded runner binaries and provider executables must not be served from a worker-writable cache without protected integrity and ownership guarantees.

Cache routing does not silently widen worker permissions.
The launch plan may request narrow owned runtime-cache grants, but they are visible capabilities and are checked against denials/managed requirements.
If a requested cache lies under a denied write subtree, fail or use an explicitly selected nonpersistent session cache; do not ignore the denial.

## 18. Resolution, validation, and switching

The conceptual pipeline is:

```text
parse selected inputs without executing them
  -> resolve configuration layers, definitions, and one profile
  -> normalize paths and requested capabilities
  -> apply trusted requirements and obtain limited probe authorization if needed
  -> resolve target/provider identities and probe capabilities within that authorization
  -> obtain final authorization bound to the resolved identities and effective configuration
  -> prepare approved environments/images/storage
  -> freeze the effective launch plan and recheck identities against final authorization
  -> launch through mcp-console sandbox
  -> enforce, supervise, and record the worker generation
```

A side-effect-free structural check is distinct from an authorized target probe.
Probing may resolve a Docker tag to a digest, verify an SSH host identity, or inspect an approved command-provider executable; it does not authorize preparation or launch.
If an identity changes after final authorization, stop and obtain authorization for the newly resolved identity before continuing.
Do not contact every SSH host or build every image just to list profiles.
Source provenance survives all steps: a diagnostic should name the file, profile/definition, field, requested behavior, and missing capability.

Proposed commands:

```sh
mcp-console config init
mcp-console config check
mcp-console config explain --profile remote
mcp-console profiles list
mcp-console serve --profile remote
```

`config explain` should show the selected target, workspace, provider, effective permission grants/denials, package preparation authority, secret recipients, resolved storage paths, limits, and whether trust/capability checks remain pending.
Its default operation is redacted and non-executing; an explicit probe requires authorization.
A machine-readable JSON form is useful for clients and tests, but the on-disk format remains YAML.

For the MCP interface, add a profile selection field to the existing session start/restart flow rather than immediately creating an independent collection of configuration-management tools.
A running-session profile change requires an explicit restart action.
Report that in-memory objects, connections, and services will be lost; do not silently serialize and restore arbitrary state.

Preflight and preparation should leave the old worker usable when possible.
After approval and successful preparation, stop admission, retire the old worker and its descendants/services, then start the replacement.
A failed replacement is reported as such.
Do not silently revive the old, more privileged profile as a fallback.
Account for any staged resources without exceeding controller capacity limits.

Even a permission reduction requires replacement: an existing process may already hold open files, sockets, loaded code, or sensitive memory.
File edits do not hot-reload into a running session.
Approved changes become effective at a new generation boundary.

## 19. Schema evolution and implementation slices

### Review boundary

This sketch chooses terminology, common examples, permission semantics, and ownership boundaries; it is not an exhaustive provider or security implementation specification.
Correct contradictions in those choices here.
Settle exact wire formats, locking algorithms, option allowlists, and platform enforcement in the PR that implements each feature, with tests.
A missing implementation detail can justify deferral, not silently reinterpreting a promised restriction.
When implementation exposes a better design, revise the proposal explicitly rather than accumulating exceptions.

### Parsing contract

Use YAML 1.2-compatible scalars, mappings with string keys, and sequences.
Reject duplicate keys, unknown schema keys, custom tags, executable constructors, merge keys, and aliases/anchors initially.
Bound input size, nesting, and expansion.
Quoted duration/size strings are accepted alongside unquoted strings; booleans must be actual booleans, not strings that happen to look affirmative.

`version: 1` fixes semantic meaning, not the list of implemented providers.
Validate unknown keys in every declared profile.
Check runtime capabilities only for the selected profile; a valid but unsupported SSH profile should not prevent use of a local profile.
An older parser that does not recognize a future field must reject it rather than run while ignoring it.
A future `min_version` may improve diagnostics but does not replace strict parsing.

Provider-specific settings belong in that provider's validated `options` object.
They cannot shadow common permission keys or become an unvalidated back door.
Diagnostic metadata, if needed, can live under a clearly inert `metadata` object, not under ignored policy-looking keys.

Support data-file references only where their schemas define them.
Do not start with an unbounded include system, remote includes, YAML templating, or arbitrary merge scripts.
Keeping a Dockerfile or lockfile beside the main config is sufficient for the initial use cases.

### Suggested sequence of pull requests

| Slice | Deliverable                                                                                                                             | Deliberate limit                                                          |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1     | Strict YAML parser, implicit/default and built-in profiles, normalized configuration, `check`/`explain`, file-discovery and trust model | Only currently enforceable launches; reject unsupported selected behavior |
| 2     | Filesystem grants/denials, protected control inputs, exact-path semantics, starter config                                               | Do not ship the workspace-write starter until its denials are enforced    |
| 3     | Managed outbound proxy and exact origin/host-port rules                                                                                 | No HTTPS path filtering claimed through opaque tunnels                    |
| 4     | Language selection, resolution controls, explicit manifests and repository policy                                                       | Preserve current activation contracts; disclose existing host-build trust |
| 5     | Storage routing and safe retention, scheduling hints, provider capability reporting                                                     | No recursive deletion of arbitrary shared caches                          |
| 6     | SSH target, approved setup, remote manager lifecycle, path mapping                                                                      | Existing remote workspace; no implicit synchronization                    |
| 7     | Services/forwarding and exact local-IPC grants                                                                                          | No blanket localhost/LAN allowance                                        |
| 8     | Docker, Docker Sandbox, and versioned command adapters                                                                                  | No silent policy dilution by provider flags                               |
| 9     | Hard resource limits, concurrent-session admission, additional SQL adapters, stronger URL filtering                                     | Each selected capability fails until enforcement exists                   |

These are coherent implementation areas, not a requirement to make exactly nine PRs.
Split further where needed.
In particular, parser support does not constitute security-feature implementation.

The first usable config can be as small as `version: 1` plus `extends:
read_only`, translating to today's boundary.
The richer ten-line starter becomes available only once write allowances and its read/write denials work.
Before that point, `config init` must generate only an enforceable template or clearly refuse the requested template.

### Acceptance tests that preserve the design

Test the public launcher/workflow boundary, with ordinary cases running on every supported platform and explicit capability requirements for specialized cases.
Avoid OS-name allowlists where the actual requirement is a sandbox feature.

Important cases include inherited denial preservation; a denied nested file under a writable root; new files matching a deny glob; symlink and directory- replacement attempts; config modification after approval; a spoofed or changed provider; proxy bypass and crash; private-address/DNS changes; an ordinary database driver through the permitted route; Shiny publishing across a namespace and SSH; disconnect cleanup; container mount/policy intersection; blocked public package fallback; disabled resolution; active-cache pinning; concurrent capacity reservation; and limits covering descendants rather than only the initial PID.

Also test profile-switch failures without silent fallback, full-access reporting, secret redaction in diagnostics, and semantic equality/provenance in `explain`.
Keep repetitive initialization payloads out of per-case transcript snapshots.

For each proposed shorthand, test equality with its expanded mapping under inheritance, including a parent of the same kind.
Also distinguish an extensible built-in default from an explicit managed ceiling, and an approved definition from an unapproved request to select or modify it.
Parsing the examples alone does not verify these semantics.

## 20. Alternatives and decisions worth revisiting

### YAML versus TOML

**Recommended: YAML first.** It keeps nested targets, connection maps, rule objects, comments, and inline Dockerfiles readable in one file.
A strict subset avoids treating YAML as a programming language.

**Reasonable alternative: TOML only.** It aligns with Codex's file format and has fewer YAML-specific parsing surprises.
The cost is more table syntax for profiles and arrays of network/compute objects.
The semantic model above does not depend on YAML, so switching the serialization now would not invalidate the design.

**Another alternative: both with exact semantic parity.** This helps users who strongly prefer one, but doubles parser/editor/docs testing.
Do not auto-merge `config.yaml` and `config.toml`; their coexistence must be an error or require an explicit selection.
Never let one format express an authority feature the other cannot represent.
Defer dual-format support until it earns its cost.

### Allow/deny lists versus a Codex-style access map

**Recommended:** `allow_write`, `deny_write`, and `deny_read`, with deny-wins semantics.
They make the highest-frequency edits obvious and avoid order-dependent review.

**Reasonable alternative:** a path-to-access map:

```yaml
permissions:
  filesystem:
    paths:
      ".": write
      ".git": read
      ".agents": read
      ".codex": read
      "~/.ssh": deny
```

This is more compact for mixed subtrees and closer to Codex's `read`/`write`/ `deny` model.[1] It requires choosing and documenting specificity, tie-breaking, and reopening semantics.
It is attractive when “deny a broad tree, then reopen one nested area” becomes common.
Do not expose both syntaxes initially with slightly different precedence.

**Not recommended initially:** ordered interleaving of `allow` and `deny` rules.
It is expressive but makes inheritance and review harder: moving a line or adding a late broad rule can change many earlier restrictions.
An explicit rule removal is easier to explain and authorize.

### Complete profiles versus mandatory orthogonal registries

**Recommended:** a complete inline default profile, optional named profiles, and optional reusable component definitions.
This keeps most files short while supporting an SSH/Docker/environment matrix when a user actually needs one.

**Reasonable alternative:** require separate `targets`, `policies`, `environments`, and `profiles` catalogs.
It eliminates duplication in larger team configurations, but makes a simple “write here, deny this, allow that API” file unnecessarily indirect.

**Reasonable smaller alternative:** profiles only, with no `definitions` in the first release.
Copy a few target blocks until real duplication justifies the extra machinery.
Keep the top-level schema slots reserved, rather than implementing an elaborate reference system preemptively.

### Separate target and sandbox versus one provider

**Recommended:** separate them.
SSH answers where; Docker answers what compute container; a sandbox answers which permissions are enforced.
They can compose, subject to capability checks.

**Reasonable alternative:** a single `execution.provider` owns everything, including remote placement and security.
This is simpler for a single vendor sandbox, but either duplicates SSH+Docker combinations or requires inventing a nested provider language.
Even with one user-facing provider, retain the separation in the normalized internal model.

**Another reasonable alternative:** one tagged `run_on` node for local, SSH, Docker, Docker Sandbox, or command execution, with one optional `inside` container.
The earlier configuration exploration used this approach.[15] It hides the transport/compute distinction in ordinary files, at the cost of less uniform composition.
Prefer it if the simpler entry point outweighs independently reusable transport and compute settings.
URI shorthands are optional: a short mapping with an explicit remote workspace avoids inventing an implicit workspace or synchronization policy.
Keep any shorthand equivalent under inheritance, not merely when read in isolation.

### Strict project approval versus trusted-workspace auto-loading

**Recommended:** approve effective configuration and target identity, and reapprove changes that expand or materially alter authority.

**Reasonable alternative:** explicit opt-in to trust all configuration changes in a workspace.
It is convenient, but gives every tool/process that can edit that configuration the ability to request new host execution authority.
It is not a safe default for an agent-writable project.
A delegation policy limited to specific roots/endpoints is narrower than trusting all future edits.

### Default package behavior

**Recommended:** `resolution: explicit`, preserving declarative initial preparation but disabling surprise missing-import installs.

**Reasonable alternative:** retain `automatic` for compatibility with the current workflow, with visible separate preparation authority and strict source controls.
This is a product/workflow decision, not a reason to conflate worker network permissions with installer access.

### Exact URL filtering now versus destination filtering first

**Recommended:** implement origins and exact TCP endpoints first; reserve the stronger typed URL rule for a capable proxy.
They cover the common API and database cases without silently breaking certificate assumptions.

**Reasonable alternative:** make TLS-terminating mediation a first-class early provider and offer path/method rules immediately.
It provides finer HTTP controls but adds certificate distribution, mTLS/pinning compatibility, normalization, and auditing concerns.
The file format above accommodates it without changing the meaning of existing origin rules.

### What should stay out of version 1

Do not build a general orchestration DSL, unrestricted shell interpolation, multiple inheritance, arbitrary provider composition graphs, automatic project synchronization, a package-manager replacement, or live permission mutation.
The configuration should describe a session and its requested capabilities, not become another programming language.

## References

External details were checked on 2026-09-09.
Upstream pages are evolving; the normative recommendations in this document are MCP Console proposals rather than claims of source compatibility.

| Source | Reference                                                                    |
| ------ | ---------------------------------------------------------------------------- |
| 1      | [Codex permission profiles and rules][1]                                     |
| 2      | [Anthropic Sandbox Runtime README][2]                                        |
| 3      | [MCP Console README at the inspected commit][3]                              |
| 4      | [MCP Console CLI at the inspected commit][4]                                 |
| 5      | [Shiny `runApp` reference][5]                                                |
| 6      | [Docker Sandbox security model][6]                                           |
| 7      | [MCP Console requirements and environments][7]                               |
| 8      | [uv settings and index strategy][8]                                          |
| 9      | [DuckDB configuration reference][9]                                          |
| 10     | [R user directories][10]                                                     |
| 11     | [renv cache and library paths][11]                                           |
| 12     | [pkgcache configuration][12]                                                 |
| 13     | [uv environment variables][13]                                               |
| 14     | [uv cache behavior and maintenance][14]                                      |
| 15     | [Earlier configuration exploration, PR #156 (historical, not normative)][15] |

[1]: https://developers.openai.com/codex/permissions
[2]: https://github.com/anthropics/sandbox-runtime/blob/main/README.md
[3]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/README.md
[4]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/src/cli.rs
[5]: https://shiny.posit.co/r/reference/shiny/latest/runapp.html
[6]: https://docs.docker.com/ai/sandboxes/security/
[7]: https://github.com/t-kalinowski/mcp-console/blob/c3027d71a86f837804ff3234dd1fab5c9103ff40/docs/REQUIREMENTS.md
[8]: https://docs.astral.sh/uv/reference/settings/
[9]: https://duckdb.org/docs/current/configuration/overview.html
[10]: https://stat.ethz.ch/R-manual/R-devel/library/tools/help/R_user_dir.html
[11]: https://rstudio.github.io/renv/reference/paths.html
[12]: https://github.com/r-lib/pkgcache
[13]: https://docs.astral.sh/uv/reference/environment/
[14]: https://docs.astral.sh/uv/concepts/cache/
[15]: https://github.com/t-kalinowski/mcp-console/pull/156
