# Docker execution

**Status:** Implemented for macOS and Linux controllers and Linux images.

The MCP server and recordings stay on the controller.
Each worker generation gets a fresh Console-owned container containing both the relay and built-in worker.
Docker image setup happens once before MCP readiness.
R, Python, Console, its companion bundle, and analysis packages must come from the image; this mode never prepares packages dynamically.

## Build an image and start a session

The [example Dockerfile](../examples/docker/Dockerfile) builds the checked-out Console source and pinned companion bundle for the image's Linux architecture.
It installs shared-library R, current reticulate, R DBI/DuckDB/Arrow/nanoarrow/jsonlite, the pillar/tibble/utf8 SQL printer dependencies, and a Python environment with NumPy, pandas, Matplotlib, and DuckDB.
Arrow includes Dataset support, which DuckDB's scanner needs for SQL previews.
Building requires registry, package repository, and source repository access.
It does not copy the controller's executable or R/Python libraries.

From this repository's root:

```sh
docker build -f examples/docker/Dockerfile -t my-console:analysis .
```

The repository's `.dockerignore` excludes build output and Console session storage.
Docker's ordinary context handling and `.dockerignore` rules apply to all builds.
Choose the daemon and build platform through Docker's existing configuration; Console does not infer a container architecture from its controller.

In the project where the controller will run, save this complete `.agents/console/config.yaml`:

```yaml
target:
  transport: {kind: local}
  workspace: /workspace
  command: [mcp-console]
  compute:
    kind: docker
    image: my-console:analysis
    pull: if_missing
    mounts:
      - source: .
        target: /workspace
        access: read_write
sandbox:
  filesystem:
    kind: external-sandbox
  network: enabled
```

Start `mcp-console serve` normally through your MCP client.
This selects Docker as the outer isolation boundary, with ordinary bridge networking.
The project mount permits writes, including to `.agents/console/sessions/`; external mode supplies no native metadata protection.
The controller still owns recording and writes the returned image bytes and output spools at controller paths.

To build once during initial session setup instead, use:

```yaml
target:
  workspace: /workspace
  compute:
    kind: docker
    build:
      context: /path/to/mcp-console
      dockerfile: /path/to/mcp-console/examples/docker/Dockerfile
    mounts:
      - source: .
        target: /workspace
        access: read_write
sandbox:
  filesystem: {kind: external-sandbox}
  network: enabled
```

Both build paths refer to the controller.
They may be outside the project.
Console builds before any analysis worker starts and never reads the inputs again for that session.
It does not implement the design sketch's proposed provider access-query API or protected-input evaluator.
Builds and pulls are trusted setup operations with their own network access and Docker's existing registry authentication.
Their diagnostics use stderr, independently of MCP and relay protocol stdout.

## Configuration reference

Only `.agents/console/config.yaml` in the controller's fixed launch directory is discovered.
There is no ancestor search, container-side discovery, interpolation, tilde expansion, reload, or file synchronization.
The captured target, raw native policy, CLI writable roots, and recording directory remain fixed for the session.

| Field                             | Accepted values and default                                                                                                                                                                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `target.transport`                | `{kind: local}` for Docker; omission means local. SSH plus Docker is unsupported.                                                                                                                                                           |
| `target.compute.kind`             | `docker`; omitted compute means `{kind: host}`.                                                                                                                                                                                             |
| `target.workspace`                | Required absolute existing container directory. Checked after binds are applied. Console starts the launcher in `/` and does not use Docker's working-directory creation behavior to create this path.                                      |
| `target.command`                  | Nonempty argv prefix executed inside the image; default `[mcp-console]`. No shell is inserted. The prefix must select a compatible Console installation and reserve stdout for the launch protocol. It does not implicitly install Console. |
| `target.compute.image`            | An existing image reference. Required unless `build` is supplied; mutually exclusive with `build`.                                                                                                                                          |
| `target.compute.pull`             | `never`, `if_missing` (default), or `always`. Valid only with `image`. Refresh happens only at initial setup.                                                                                                                               |
| `target.compute.build.context`    | Required existing controller directory; relative to the controller launch directory.                                                                                                                                                        |
| `target.compute.build.dockerfile` | Required existing controller file; relative to that same launch directory, independently of `context`. No inline Dockerfile form.                                                                                                           |
| `target.compute.mounts`           | Optional list of explicit bind mounts; default `[]`.                                                                                                                                                                                        |
| `mounts[].source`                 | Required bind source. Relative values become absolute against the controller launch directory; Docker checks existence on its daemon host. Missing sources fail rather than creating directories.                                           |
| `mounts[].target`                 | Required absolute container path.                                                                                                                                                                                                           |
| `mounts[].access`                 | `read_only` (default) or `read_write`.                                                                                                                                                                                                      |
| `target.compute.user`             | Optional Docker user/group string, such as `1000:1000`. Omission respects the image's configured user.                                                                                                                                      |

Unknown Console target fields and unsupported combinations are errors.
Native sandbox fields remain raw values for native runner validation; this target schema adds no native-policy allowlist.

Omitting `target` keeps the existing local host path.
An explicit local transport with host compute selects that same path and uses the controller launch directory and built-in command; `workspace` and `command` overrides require SSH or Docker.
Existing SSH configurations retain their `[uvx, mcp-console]` default command.

There are no implicit home, project, credential, agent, or Docker socket binds.
Docker mount arguments preserve spaces, commas, and quotes using Docker's CSV grammar.
Docker ultimately interprets sources on its daemon host: Docker Desktop supplies normal host sharing, while a remote daemon does not receive controller files merely because a source names them.
The CLI transfers build contexts through its ordinary build interface.

Console preserves Docker CLI context and authentication configuration and captures the selected daemon endpoint and TLS selection for all session lifecycle operations.
Changing the active context later does not redirect cleanup.
Workload environment settings never configure Docker, image builds, or the trusted launcher.

Bind access and ordinary Unix ownership both apply.
Container UIDs can own files created in a writable bind.
Select an appropriate image user or `compute.user` when this matters; Console does not chown the mounted tree.

## Environment and sandbox selection

Setup checks Console package/protocol compatibility, the container workspace, R discovery and shared `libR`, explicit Python selection, and native preflight before announcing readiness.
It does not start the analysis worker during this probe.
Missing analysis packages retain ordinary package or adapter errors.
Install them in the Dockerfile and start a new server session.

Docker deliberately selects the existing bare-runtime capability even when `uv` or `ir` is present.
The tool schema omits `requirements`; explicit requests and worker callbacks cannot run controller resolvers.
Reticulate's implicit managed-environment installation is disabled.
No mutable preparation container, package volume, or automatic image mutation is provided.
Future container preparation must have a separate trusted lifecycle from the relay and worker.

The workload starts with image environment defaults plus `sandbox.environment` and `sandbox.inherit_environment`.
R and Python selections are resolved inside the container.
Image or explicit target `R_HOME` and `RETICULATE_PYTHON` selections and Console's runtime variables take precedence over conflicting workload controls; malformed native values still reach target-side validation.
The controller's `HOME`, `TMPDIR`, library paths, and interpreter selections are never forwarded.

Default native sandbox selection is unchanged.
Native enforcement runs through the public `mcp-console sandbox` boundary inside the container, with a container-local owner PID.
Built-ins, literal policy paths, runtime paths, proxy addresses, and CLI `--writable-root` refer to the container namespace.
Writable binds are an upper bound: native read-only entries, metadata defaults, and exceptions retain the runner's semantics.

Ordinary Docker security settings can deny the namespace operations required by bubblewrap.
Console forwards that native setup error.
It does not add privileged mode, `SYS_ADMIN`, host namespaces, unconfined security profiles, or a Landlock fallback.
See [Linux compatibility](LINUX_COMPATIBILITY.md) for the native capability requirements.
A daemon that rejects nested native enforcement can use the explicit external configuration above.

Without a proxy, `external-sandbox` delegates both filesystem and network enforcement to Docker.
Native filesystem entries do not narrow this mode; `network: restricted` installs no network block.
Docker binds, normal bridge networking, namespaces, and container retirement provide the outer boundary.
An explicitly configured proxy still uses the native path and can fail native setup.
No additional outer networking modes are implemented.

`serve --no-sandbox` retains Docker target selection and workload environment controls.
It skips the inner native sandbox while retaining the outer owned container and its retirement.
Standalone `mcp-console sandbox -- COMMAND` remains local regardless of `target`.

## Image identity, lifetime, and records

Initial setup resolves an immutable image ID and records it with the requested image reference or build paths and a repository digest when one is available.
Locally built images may have no repository digest.
Every probe and worker generation uses that captured ID.
Editing YAML, retagging an image, or editing a Dockerfile cannot change a restart or failure replacement; start a new server session to consume those changes.

Every generation owns a newly created container.
Bind-mounted files persist across retirement.
Changes in the container's writable layer disappear, including files in container-local temporary directories.
The adapter does not attach to or adopt existing user containers.

Creation precedes attachment and yields an authoritative container ID before any workload starts.
A unique ownership label identifies partial creation.
Launch overrides the image entrypoint, disables healthchecks and automatic restart, selects SIGTERM for stopping, and enables Docker init/reaping.
Attachment is non-TTY, with open stdin and separate stdout/stderr.
Cell interrupts go through the existing relay protocol to the worker; they do not stop the container.

A local ownership helper watches controller input closure and termination independently of blocked output.
Loss of the Docker attachment also initiates retirement.
The helper requests bounded normal stop, then forced removal, including image-declared anonymous volumes, and requires a successful daemon query proving the exact container absent.
Docker CLI exit, EOF, and automatic removal are not cleanup receipts.
Unknown cleanup prevents replacement and reports the owned container identity.
If creation returns no ID and the ownership label cannot identify a container, an empty listing cannot confirm that an in-flight creation has retired.
Console reports that uncertainty with the ownership name; a delayed daemon operation may leave a stopped container requiring removal once communication is restored.
Submitted cells are never replayed after transport failure.

Setup pulls and builds are cancellable without a short build deadline; image/build caches may remain.
Daemon commands and container retirement have bounded waits.
After daemon, host, or ownership-helper failure, Console cannot guarantee cleanup that it cannot confirm.
There is no reconnect, resume, later attachment, heartbeat, or recovery after the local owner itself is killed.

The controller journal records target identity separately from its recording directory and includes generation container IDs.
It records no credential values or environment dump.
Journals, transcripts, output spools, and returned image bytes remain beneath the controller project's `.agents/console/sessions/`.
Declared binds may expose that directory to the workload.
Other files remain inside the container or declared binds.

Quarto projections identify Docker, omit an incorrect controller execution root, and default to `execute.eval: false`.
Deliberately recreate the target environment and filesystem before enabling execution.

## Acceptance tests and deferred work

Public Docker cases use the example image and an accessible Linux daemon.
Build the fixture before running tests and select it with `MCP_CONSOLE_TEST_DOCKER_IMAGE`.
The fixture build is outside individual test deadlines and may compile large R dependencies.
A missing daemon or unselected fixture skips these integration cases and supplies no Docker validation evidence.
Controllers can run these tests on macOS or Linux; the image remains Linux.

The September 12, 2026 validation used macOS 26.6.2 arm64 (Darwin 25.6.0) and a Debian 12 arm64 controller container against Docker Engine 29.5.2 in Colima 0.10.3, with kernel `6.8.0-117-generic`.
Both controllers used Docker CLI 29.8.0; Linux also exercised Buildx 0.37.0 with BuildKit.
Containers used ordinary daemon security settings, with no added capabilities or security-profile overrides.
Native enforcement and explicit proxy setup failed with bubblewrap's namespace-permission error on this daemon; nested native permission success was not established.
The permission assertions are enabled only when a real native probe succeeds.
Pull-policy branches use a deterministic registry CLI peer backed by real daemon images; build, container, and lifecycle operations use the real daemon.
Cancellation of a running build is covered through the legacy builder's observable build container; BuildKit is covered by the captured-build test.

```sh
docker build -f examples/docker/Dockerfile -t mcp-console-test:analysis .
export MCP_CONSOLE_TEST_DOCKER_IMAGE=mcp-console-test:analysis
scripts/test cli/test_docker client_server/server/test_docker client_server/server/test_docker_setup client_server/server/test_docker_lifecycle client_server/server/test_docker_sandbox
scripts/check
```

Deferred capabilities include SSH plus Docker, Docker Sandbox microVMs, Podman guarantees, Windows containers or controllers, inline Dockerfiles, GPU/device/resource controls, arbitrary Docker flags, managed package preparation, synchronization, reconnect/resume, and reusable user containers.
Transport and compute remain separate fields for future composition.
