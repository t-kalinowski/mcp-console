# Docker execution

Ordinary Docker runs the relay and worker in a new owned Linux container for each generation.
The MCP server and [recordings](RECORDING.md) stay on a macOS or Linux controller.
This is separate from [Docker Sandbox microVMs](DOCKER_SANDBOX.md).

Docker targets use preinstalled runtimes and packages; Console does not resolve dependencies in a running container.
Image setup and the runtime probe run in the background without blocking MCP initialization or tool discovery.
The accepted image ID and runtime selection remain fixed for the session.

## Build an image and start a session

From the repository root, build the [mixed-language image](../examples/docker/Dockerfile):

```sh
docker build -f examples/docker/Dockerfile -t my-console:analysis .
```

It builds Linux Console and its pinned native companion, and installs R, Python, and analysis dependencies.
Do not copy controller executables or libraries into the image.
Builds use Docker's context and `.dockerignore`, registry authentication, and network access.

In the controller project, create `.agents/console/config.yaml`:

```yaml
target:
  workspace: /workspace
  compute:
    kind: docker
    image: my-console:analysis
    pull: if_missing
    mounts:
      - source: .
        target: /workspace
        access: read_write
sandbox:
  filesystem: {kind: external-sandbox}
  network: enabled
```

Start `mcp-console serve` through an MCP client.
This example explicitly delegates enforcement to Docker and uses its ordinary bridge networking.
The writable project bind includes metadata and recordings beneath it: external mode adds no native `.git` or `.agents` protection.

## Configuration reference

[Configuration discovery](CONFIGURATION.md) runs only on the controller.
Paths are captured once; there is no container-side configuration discovery, synchronization, interpolation, or reload.

| Field                   | Contract                                                                                                                                    |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `target.transport`      | Omitted or `{kind: local}`. SSH plus Docker is unsupported.                                                                                 |
| `target.workspace`      | Required absolute directory that exists in the container after binds. Console does not create it.                                           |
| `target.command`        | Nonempty in-image Console argv prefix; default `[mcp-console]`. No shell or implicit installation.                                          |
| `target.compute.kind`   | `docker`.                                                                                                                                   |
| `target.compute.image`  | Image reference; mutually exclusive with `build`.                                                                                           |
| `target.compute.pull`   | `never`, `if_missing` (default), or `always`; applies only during initial image setup.                                                      |
| `target.compute.build`  | Mapping with required `context` directory and `dockerfile` file on the controller. Both resolve independently against the launch directory. |
| `target.compute.mounts` | Explicit binds, default `[]`; no implicit project, home, credential, or Docker socket mount.                                                |
| `mounts[].source`       | Required source, made absolute against the controller launch directory; Docker checks it on the daemon host.                                |
| `mounts[].target`       | Required absolute container path.                                                                                                           |
| `mounts[].access`       | `read_only` (default) or `read_write`.                                                                                                      |
| `target.compute.user`   | Optional Docker user/group string; otherwise use the image user.                                                                            |

To build during initial setup, replace `image` and `pull` with:

```yaml
build:
  context: /path/to/mcp-console
  dockerfile: /path/to/mcp-console/examples/docker/Dockerfile
```

No arbitrary Docker arguments, inline Dockerfiles, GPU/resource controls, or existing-container adoption are exposed.
Unknown target fields and unsupported combinations fail validation.

Docker interprets bind sources on its daemon host: a remote daemon does not receive controller files merely because a bind names them.
Normal ownership and access permissions apply; Console does not chown the tree.
Mount arguments preserve spaces, commas, and quotes through Docker's CSV grammar.
The daemon endpoint and TLS selection are captured so changing the active CLI context cannot redirect cleanup.
Workload environment settings do not configure Docker or image builds.

## Environment and sandbox selection

A disposable probe checks Console package/protocol compatibility, the existing workspace, native policy where applicable, and installed runtimes.
The descriptor is accepted only after confirmed probe-container removal.
It does not initialize an analysis interpreter or open a SQL catalog.

R discovery uses the effective workload `R_HOME` and `PATH`; a broken selected installation is an error.
Without R, Console selects preinstalled CPython with a usable shared embedding library.
With R but no Python executable, R and SQL remain available.
Python must be 3.10 or later.

Top-level `python` takes precedence over image/workload `RETICULATE_PYTHON`.
Relative paths resolve against `target.workspace`; otherwise discovery tries `python3`, then `python`, on the effective workload PATH.
An invalid selected executable does not trigger fallback.
The controller does not discover or load these target paths.
Workload environment controls apply inside the container; retained runtime selections override conflicting layout variables.

`requirements.action="get"` returns the declaration, not an image package inventory.
Requirement mutations, automatic resolution, and reticulate's implicit environment installation are disabled even when `uv` or `ir` is installed.
Rebuild the image and start a new server session to change dependencies.

Native enforcement remains the default.
Its policy, writable roots, proxy addresses, and parent PID belong to the container namespace.
Ordinary Docker security settings may reject nested bubblewrap namespaces; Console reports the error without adding privileges or switching backends.

Without a proxy, `external-sandbox` delegates both filesystem and network enforcement to Docker.
Native entries do not narrow access, and `network: restricted` does not create a network block in this mode.
A supplied proxy still requires native setup.
`serve --no-sandbox` skips the inner native runner but retains the owned container and retirement.

### Prepared Python without R

Build the [R-free image](../examples/docker/Dockerfile.python):

```sh
docker build -f examples/docker/Dockerfile.python -t my-console:python .
```

Select that image in the configuration above.
It supplies Python analysis packages, the shared embedding library, and an example preinstalled `fts` extension, with no R or reticulate.
An explicit selection can use `python: /opt/console-python/bin/python`.

Python and SQL use the [shared runtime](BUILTIN_RUNTIME.md).
Missing DuckDB leaves Python and custom DB-API connections usable.
SQL spill files and stored secrets use disposable worker storage; preinstalled extension caches and binds retain their locations.
No packages or extensions are installed automatically.

## Image identity, lifetime, and records

Initial setup captures an immutable image ID, plus a repository digest when available.
Every probe and generation uses that ID.
Retagging an image or editing its Dockerfile cannot change restart or crash replacement.
Each generation has a fresh writable layer; only bind-mounted files persist.

Creation obtains an authoritative container ID before attachment starts the workload.
A unique ownership label tracks partial creation.
Console overrides the entrypoint, disables healthchecks and automatic restart, enables Docker init, and attaches without a TTY.
Cell interrupts go to the worker through the relay; they do not stop the container.

The local owner observes controller loss and attachment failure independently of blocked output.
It requests bounded stop, then forced removal including anonymous volumes, and queries the daemon to confirm the exact container is absent.
CLI exit, EOF, and automatic removal are not cleanup receipts.
Unconfirmed retirement blocks replacement and reports the owned identity.
An empty listing after an unacknowledged create cannot prove that a delayed daemon operation will not create a container later.

Builds and pulls are cancellable and may leave caches.
Host, daemon, or owner failure can leave resources requiring manual recovery; there is no independent recovery, reconnect, or adoption path.
Cells are never replayed after transport failure.

Target metadata records image and generation container identities separately from controller recording paths.
A writable bind can expose controller records to the worker.
Other files remain in the container or explicit binds; exports do not reproduce that filesystem.

## Acceptance tests

Select prepared fixtures explicitly:

```sh
export MCP_CONSOLE_TEST_DOCKER_IMAGE=my-console:analysis
export MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE=my-console:python
scripts/test client_server/server/test_docker \
  client_server/server/test_docker_setup \
  client_server/server/test_docker_lifecycle \
  client_server/python/test_prepared_without_r
```

Missing daemon access or unselected images cause skips, not Docker acceptance.
Fake peers cover deterministic protocol failures, not actual container cleanup.
Real tests must verify daemon-confirmed absence and distinguish successful nested native enforcement from expected namespace rejection.
Use `scripts/check --full` for broader validation when appropriate.
