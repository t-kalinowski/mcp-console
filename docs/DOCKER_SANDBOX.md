# Docker Sandbox execution

Docker Sandboxes supplies a microVM, shared paths, policy, and host integrations through the separately installed `sbx` CLI.
Console keeps the MCP server and [recordings](RECORDING.md) on the controller and runs the relay and worker in a new owned VM for each generation.
This is not [ordinary Docker Engine execution](DOCKER.md).

The adapter requires standalone `sbx` 0.42.1 or newer; it does not use the older `docker sandbox` CLI or a private daemon API.
Console supports macOS and Linux controllers, but a working Docker Engine alone does not establish SBX or virtualization availability.

## Prerequisites and template setup

Install and configure the provider using Docker's [installation instructions](https://docs.docker.com/ai/sandboxes/install/).
Complete login, policy, and virtualization setup before launching Console.
Inspect availability without changing provider state:

```sh
sbx version
sbx ls --json
sbx policy ls --json --include-inactive
```

Console does not install SBX, log in, reset global policy, alter credentials, or restart the shared daemon.

The [example template](../examples/docker-sandbox/Dockerfile) builds Linux Console and preinstalls R, Python, and analysis packages using Docker's `shell-docker` template.
It creates `/workspace`, starts no coding agent, and requires no model-provider authentication.
Its final image contains no native companion: this provider never discovers or invokes that executable.
The template and controller need compatible Console package and target-protocol versions.

```sh
docker build -f examples/docker-sandbox/Dockerfile -t mcp-console-sandbox:analysis .
```

Build and import are explicit setup, not `serve` operations.
Use a digest-qualified registry reference after publishing to a registry accessible to SBX, or import locally:

```sh
docker image save mcp-console-sandbox:analysis -o console-template.tar
python3 examples/docker-sandbox/qualify-oci.py \
  console-template.tar console-template.oci.tar \
  docker.io/library/mcp-console-sandbox > console-template-reference.txt
sbx template load console-template.oci.tar
```

The [OCI naming utility](../examples/docker-sandbox/qualify-oci.py) verifies and names a single-platform manifest without changing its bytes or layers.
It rejects legacy tag-only archives and multi-platform indexes; use an appropriate OCI export or the registry route instead.
Docker's image store and SBX's template store are separate: a Docker image ID is not an SBX template reference.

## Complete configuration

Save this in the controller's `.agents/console/config.yaml`, replacing the paths and full 64-digit digest:

```yaml
target:
  workspace: /path/to/project
  compute:
    kind: docker_sandbox
    template: docker.io/your-org/mcp-console-sandbox@sha256:<manifest-digest>
    mounts:
      - source: /path/to/project
        target: /path/to/project
        access: read_write
sandbox:
  provider: compute
```

Run `mcp-console serve` through an MCP client.
To expose no project, omit `mounts` and use a directory already present in the template, such as `/workspace`.

| Field                         | Contract                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------- |
| `target.transport`            | Omitted or `{kind: local}`.                                                               |
| `target.workspace`            | Required absolute VM directory, existing after shares are established.                    |
| `target.command`              | Optional in-VM Console argv prefix, default `[mcp-console]`; not an SBX launcher command. |
| `target.compute.template`     | Required prepared digest-qualified registry reference, captured for the session.          |
| `target.compute.mounts`       | Explicit shares, default `[]`.                                                            |
| `mounts[].source`             | Host path; relative values resolve against the controller launch directory.               |
| `mounts[].target`             | Must equal the resolved absolute source; remapping is unsupported.                        |
| `mounts[].access`             | `read_only` (default) or `read_write`.                                                    |
| `sandbox.provider`            | `compute`, also the default for this target.                                              |
| `sandbox.environment`         | Optional string-to-string workload overrides.                                             |
| `sandbox.inherit_environment` | Boolean, default `true`; inherits the template environment, not controller runtime paths. |

Those are the complete compute-provider sandbox fields.
Native fields, top-level `extends`, and CLI writable roots are rejected, including with `--no-sandbox`.
Configure the outer boundary through provider policy and explicit shares.

Direct shares use identical host/guest paths and encode read-only access with `:ro`.
Sources containing `:` are unsupported.
The tested SBX 0.42.1 interface requires its first share to be read/write; Console never adds a writable share to work around that restriction.
It verifies returned share paths and access, never answers directory-creation prompts affirmatively, and performs no clone or synchronization.
Use the provider-resolved source path when symlinks alter share identity.

`--no-sandbox` retains the microVM, sharing, and provider policy; there is no inner native layer to disable.
Standalone `sandbox -- COMMAND` rejects compute enforcement rather than silently running locally.

## Runtime and policy

A disposable probe checks the workspace, package/protocol compatibility, and installed runtimes under the effective workload environment.
It uses the same prepared-runtime selection as Docker without initializing an analysis interpreter or SQL catalog.
R requires a usable shared library; genuine absence selects native Python, while a broken selected runtime is an error.
With R but no Python executable, R and SQL remain available.

Top-level `python` overrides template/workload `RETICULATE_PYTHON`.
Relative paths resolve against the VM workspace; without an explicit selection, discovery tries `python3`, then `python`, on the workload PATH.
The selected CPython must be at least 3.10 and have a usable shared embedding library.
Its descriptor remains fixed across generations; controller runtime paths and conflicting workload layout settings cannot replace it.

Only preinstalled dependencies are available.
`requirements.action="get"` inspects a declaration, not installed packages.
Requirement mutations, automatic resolution, and reticulate's implicit installation are disabled even when the template contains resolver commands.
Rebuild/import another template and start a new server session to change the environment.

### Prepared Python without R

Build the Python image and template, then import using the same OCI naming process:

```sh
docker build -f examples/docker/Dockerfile.python -t my-console:python .
docker build -f examples/docker-sandbox/Dockerfile.python \
  --build-arg CONSOLE_IMAGE=my-console:python -t my-console-sbx:python .
docker image save my-console-sbx:python -o console-python.tar
python3 examples/docker-sandbox/qualify-oci.py \
  console-python.tar console-python.oci.tar \
  docker.io/library/my-console-sbx > console-python-reference.txt
sbx template load console-python.oci.tar
```

Select the resulting reference as `template` and optionally set `python: /opt/console-python/bin/python`.
The final template has no R, reticulate, or native companion.
Missing optional packages retain ordinary errors; missing DuckDB leaves Python and custom DB-API connections usable.
The target launcher owns private storage for SQL spill and stored secrets; shared paths and preinstalled caches retain provider ownership.

### Isolation limits

Writable shares can expose `.git`, `.agents`, project configuration, and controller recordings.
There are no native metadata exclusions.
Recording ownership means the controller writes those files, not that a shared directory is isolated from the worker.

The provider inherits machine and organization policy; it is not necessarily offline.
Adding an allow rule does not create an exclusive allowlist, and provider policy can change during a session.
Console captures its configuration, not external governance state.
Review Docker's [policy inheritance](https://docs.docker.com/ai/sandboxes/governance/access-controls/local/) and [credentials](https://docs.docker.com/ai/sandboxes/configuration/credentials/).
Existing proxy secrets and host integrations remain part of that boundary.
Clearing `SSH_AUTH_SOCK` alone does not disable daemon-configured SSH-agent forwarding; this adapter supplies no per-VM forwarding opt-out.

## Transport, ownership, and retirement

Console allocates a unique name before creating each probe or worker VM, captures the provider UUID, verifies shares, and executes the in-VM Console prefix without a login shell or TTY.
The [target envelope](RELAY_PROTOCOL.md#target-launch-envelope) separates bootstrap, relay traffic, and cleanup receipts.
Provider diagnostics remain outside protocol stdout.

Cell interrupts travel through the relay and retain the VM.
Controller loss, failed attachment, or shutdown triggers whole-VM removal, independently of blocked output.
CLI exit is not cleanup evidence: a terminated `sbx exec` client can leave VM processes running.
The owner uses `sbx rm --force` and confirms absence through `sbx ls --json`.
This also retires processes and containers outside the relay's process group.

Exec and removal address names, so the owner checks the captured UUID and refuses a name now bound to another VM.
Concurrent manual rebinding between the check and command remains outside the CLI's identity guarantee.
Console never adopts an existing VM or removes one merely because its name has a Console prefix.

An unacknowledged create remains uncertain even after an empty listing or removal of an observed partial VM.
Failed removal, unavailable daemon, missing receipt, or identity mismatch blocks replacement and reports the owned name and UUID when known.
There is no automatic replay, reconnect, adoption, global prune, or independent recovery after owner/host failure.
Use the exact reported identity for manual recovery.

Restart uses the captured template and resets VM-local state; declared shares persist.
Records distinguish VM names/UUIDs and target paths from controller files.
Quarto exports retain source and context, not the VM or its filesystem.

## Acceptance and scope

Select real templates explicitly:

```sh
export MCP_CONSOLE_TEST_SBX_TEMPLATE="$(cat console-template-reference.txt)"
export MCP_CONSOLE_TEST_SBX_PYTHON_TEMPLATE="$(cat console-python-reference.txt)"
scripts/test client_server/server/test_docker_sandbox_runtime \
  client_server/server/test_docker_sandbox_lifecycle \
  client_server/server/test_docker_sandbox_policy \
  client_server/python/test_prepared_without_r
```

The shared R-free suite also uses `MCP_CONSOLE_TEST_DOCKER_PYTHON_IMAGE` when selected.
Fake CLI cases establish orchestration contracts, not microVM execution or cleanup.
A capability skip supplies no acceptance evidence; Linux controller acceptance requires a working SBX/virtualization host.

Network-policy tests additionally need `MCP_CONSOLE_TEST_SBX_NETWORK=1` and existing policy allowing `pypi.org:443` and `registry.npmjs.org:443` while denying `example.com:443`.
Inner-Docker retirement tests need `MCP_CONSOLE_TEST_SBX_INNER_DOCKER=1`, the shell-docker template, and permission to pull `busybox:1.37.0` inside the owned VM.
Tests alter only owned-VM rules, preserve global policy and unrelated resources, and serialize real VM fixtures to avoid memory overcommit.

Native policy translation, SSH/cloud placement, Windows, managed dependencies, automatic template builds, arbitrary provider arguments, and reused VMs are unsupported.
