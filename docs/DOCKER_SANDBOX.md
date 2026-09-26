# Docker Sandbox execution

Docker Sandboxes supplies the microVM, shared paths, policy, and host integrations.
Console constructs fixed argument arrays for the separately installed `sbx` CLI.
Users configure a target and run `mcp-console serve`; no SBX command or launcher script belongs in project configuration.
The MCP server, journals, output spools, image artifacts, and transcript writer run on the controller.
Both the relay and built-in worker execute inside a newly owned microVM for each worker generation.

This is distinct from [ordinary Docker Engine containers](DOCKER.md).
Console does not use the host Docker API to control Sandboxes, embed SBX, or call its private daemon API.
The durable configuration name is `docker_sandbox`.
This adapter requires standalone **sbx v0.42.1 or newer**, with no upper version bound.
Console uses the same CLI contract with newer releases; local acceptance tests expose incompatible changes when run with the installed SBX.
It does not support the older `docker sandbox` CLI.

## Prerequisites and template setup

Install Docker Sandboxes using Docker's [installation instructions](https://docs.docker.com/ai/sandboxes/install/).
Docker documents Apple Silicon macOS and Ubuntu 24.04 with KVM as local hosts; Console controllers support macOS and Linux.
Docker Engine availability alone does not establish Sandbox availability.
Complete the provider's login and policy setup yourself before launching Console.
Read-only checks are:

```sh
sbx version
sbx ls --json
sbx policy ls --json --include-inactive
```

Console does not install SBX, log in, select an account, initialize or reset global policy, change credentials, or restart the shared daemon.
Setup commands receive closed stdin and bounded deadlines.
Installation, login, policy, virtualization, template, and runtime failures are reported on controller stderr.

The [example Dockerfile](../examples/docker-sandbox/Dockerfile) compiles Console for Linux and installs shared-library R, reticulate, Python, NumPy, pandas, Matplotlib, and Python DuckDB.
It uses Docker's documented `shell-docker` [shell template](https://docs.docker.com/ai/sandboxes/agents/shell/), which includes an inner Docker daemon but starts no coding agent and needs no model-provider authentication.
It creates `/workspace` in the template.
Console and the template must have matching Console build and target protocol versions.
The Linux build stage stages the existing build-time companion prerequisite; the final template contains only the Console executable, with no native companion.
Running this provider never discovers, verifies, installs, probes, or executes that companion.
Do not copy a controller's macOS executable into the Linux template.

From the repository root, build an image for the Sandbox host's architecture:

```sh
docker build -f examples/docker-sandbox/Dockerfile -t mcp-console-sandbox:analysis .
```

Building requires access to source, OS, R, and Python package repositories.
Building and importing remain explicit setup steps; `serve` does neither.
See Docker's [template documentation](https://docs.docker.com/ai/sandboxes/customize/templates/).
The host Docker image store and SBX template store are separate.
A host-built image or Docker image ID is not a Sandbox template identity.

Two setup routes are available:

- Publish the prepared image to a registry you control using Docker's normal image push workflow, retain its manifest digest, and use `registry/repository@sha256:<digest>` in Console.
  The registry must be accessible to SBX under the user's existing provider setup.
- Export and import locally through `sbx template load`.
  With the tested Docker 29 OCI export, a tag-only archive does not register its digest-qualified name in SBX's separate store.
  The explicit [OCI naming utility](../examples/docker-sandbox/qualify-oci.py) verifies the manifest digest and assigns that reference in the archive's OCI index without changing manifest or layer bytes:

```sh
docker image save mcp-console-sandbox:analysis -o console-template.tar
python3 examples/docker-sandbox/qualify-oci.py \
  console-template.tar console-template.oci.tar \
  docker.io/library/mcp-console-sandbox > console-template-reference.txt
sbx template load console-template.oci.tar
cat console-template-reference.txt
```

This local workflow was exercised with Docker CLI 29.8.0, Engine 29.5.2, and sbx v0.42.1.
The utility accepts a single-platform OCI archive; a legacy tag-only export or multi-platform index is rejected.
Use a Docker OCI export or the registry route in those cases.
It is a setup utility, not a Console template builder or runtime integration.

The first implementation requires a digest-qualified registry reference.
Local `sbx template ls` exposes abbreviated IDs, and local `template inspect` does not establish a full immutable registry identity in this CLI version.
Console therefore captures the user's full reference once instead of resolving a mutable tag on every restart.
Digest-qualified references imported by the workflow above were accepted by actual `sbx create --template` calls.

## Complete configuration

In `.agents/console/config.yaml` beneath the controller launch directory:

```yaml
target:
  transport: {kind: local}
  workspace: /path/to/project
  command: [mcp-console]
  compute:
    kind: docker_sandbox
    template: docker.io/your-org/mcp-console-sandbox@sha256:<manifest-digest>
    mounts:
      - source: /path/to/project
        target: /path/to/project
        access: read_write
sandbox:
  provider: compute
  inherit_environment: true
  environment:
    ANALYSIS_MODE: interactive
```

Replace the paths and template reference with your values, including the full 64 hexadecimal digest digits.
There is no path or environment interpolation in YAML.
Run `mcp-console serve` from the controller project directory.

| Field                         | Contract                                                                                                                   |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `target.transport`            | Only `{kind: local}`; omission selects local.                                                                              |
| `target.workspace`            | Required existing absolute directory inside the VM, after shared paths are established.                                    |
| `target.command`              | Optional nonempty argv prefix for Console inside the VM; defaults to `[mcp-console]`. It does not select the SBX launcher. |
| `target.compute.kind`         | `docker_sandbox`.                                                                                                          |
| `target.compute.template`     | Required prepared, digest-qualified registry reference. Captured once per server session.                                  |
| `target.compute.mounts`       | Optional list; omission or `[]` shares no project directory.                                                               |
| `mounts[].source`             | Host path; relative values resolve against the fixed controller launch directory.                                          |
| `mounts[].target`             | The same absolute path as the resolved source; remapping is rejected.                                                      |
| `mounts[].access`             | `read_only` (default) or `read_write`.                                                                                     |
| `sandbox.provider`            | `compute`; this is also the default for `docker_sandbox`. `native` is unsupported for this target.                         |
| `sandbox.environment`         | Optional mapping of string names to string workload values.                                                                |
| `sandbox.inherit_environment` | Optional Boolean, default `true`; inherits the VM template's workload environment, not controller R/Python selections.     |

These are the complete accepted compute-provider sandbox fields.
Every other `sandbox` key is rejected, including filesystem modes and entries, network, proxy, native backend settings, and metadata options.
Top-level `extends` and CLI `--writable-root` are also rejected, including with `serve --no-sandbox`.
Use Docker's own policy tools and the declared shared paths to configure its boundary.
Native selections retain native passthrough and validation.

The CLI's direct shares use the same absolute host and guest paths.
Console passes read-only shares with the documented `:ro` suffix, and checks the provider's returned shared paths after creation.
In sbx v0.42.1, **the first shared path must be read/write**; subsequent paths can be read-only.
Console does not add a writable share to work around that provider restriction.
Sources containing `:` cannot be represented by this slice because SBX reserves it for access suffixes.
Spaces, quotes, and shell metacharacters are argument data.
SBX reports missing paths and access errors; Console never answers a create-directory prompt affirmatively.
It does not create the target working directory, remap symlinks, copy a checkout, clone, or synchronize files.
Use the provider's resolved absolute source path when symlinks would change the resulting share path.

A mountless template-provided workspace is supported by sbx v0.42.1:

```yaml
target:
  workspace: /workspace
  compute:
    kind: docker_sandbox
    template: docker.io/your-org/mcp-console-sandbox@sha256:<manifest-digest>
sandbox:
  provider: compute
```

Configuration discovery stays on the controller and is captured once.
Edits to YAML during a session affect only a later `serve` invocation.
`serve --no-sandbox` skips an inner native runner where applicable; this provider already launches directly inside its microVM.
The flag retains the microVM, shared-path access, and Docker policy.
Standalone `sandbox -- COMMAND` remains local for supported native selections and rejects a resolved compute selection, including the implicit selection above.

## Runtime and policy

The in-VM launcher verifies the existing working directory and compatible runtime, applies workload environment controls, and starts the ordinary relay and worker directly.
R needs a loadable shared library; Python must satisfy Console's existing image-runtime compatibility checks.
Explicit `R_HOME` and `RETICULATE_PYTHON` are VM paths.
Image environment is preserved by default.
Workload controls are applied inside the VM, after the SBX CLI has launched, and cannot configure the controller CLI or daemon.

Use preinstalled packages.
`requirements` is omitted from the tool schema and rejected if supplied.
Console never discovers controller interpreters or runs controller resolvers for this target.
Dynamic resolution and reticulate's implicit managed installation stay disabled even when `uv` or `ir` is installed in the template, or YAML tries to enable Console's private dynamic-resolution variable.
To change packages, prepare another template and start another server session.

The example supports this mixed session:

```r
x <- 41
```

```python
import duckdb

connection = duckdb.connect()
console_sql_connection(connection)
print(r.x + 1)
```

```sql
SELECT 6 * 7 AS answer
```

Docker's enforcement and permissions differ from the native runner's.
Console does not apply native default filesystem/network payloads or translate native rules.
Writable shares can include `.git`, `.agents`, project configuration, and controller recordings beneath them.
Those files receive no native metadata exclusions in this mode.
Controller recording ownership describes who writes the files; it does not imply filesystem isolation from a shared directory.

The provider inherits existing machine and organization rules.
An inherited development policy may allow package repositories and other services; it is not an offline profile.
Adding one allow rule does not create an exclusive allowlist.
Existing allows remain relevant, and matching denies take precedence under Docker's policy rules.
Organization governance can change which local allows are effective.
Console captures its own settings, not a frozen copy of externally managed policy; provider policy may change during a session.
See Docker's [local policy and inheritance](https://docs.docker.com/ai/sandboxes/governance/access-controls/local/).

Existing proxy secrets and host integrations remain part of the provider boundary.
SSH-agent forwarding can come from daemon settings as well as `SSH_AUTH_SOCK`; clearing one environment variable does not guarantee credential isolation.
The verified create/exec interface exposes no per-sandbox SSH-agent opt-out used by this adapter.
Console does not change global forwarding or credentials, import secrets, add shared skills, expose ports, or register a host MCP gateway.
Review the provider's [credential and SSH-agent configuration](https://docs.docker.com/ai/sandboxes/configuration/credentials/).

## Transport, ownership, and retirement

For each disposable readiness probe and each worker generation, Console allocates a collision-resistant name before calling the provider:

```text
sbx ls --json
sbx create --quiet --name OWNED --template DIGEST shell SHARES...
sbx ls --json
sbx exec -i --workdir CWD OWNED mcp-console INTERNAL-TARGET-OPERATION
sbx ls --json
sbx rm --force OWNED
sbx ls --json
```

These are fixed argument arrays; the configured in-VM Console prefix replaces `mcp-console`.
No command uses a login shell, `sbx run shell`, or TTY allocation.
Console checks the minimum CLI version before using the [create](https://docs.docker.com/reference/cli/sbx/create/) and [exec](https://docs.docker.com/reference/cli/sbx/exec/) contracts.
The shared [target launch envelope](RELAY_PROTOCOL.md#target-launch-envelope) carries the bootstrap, compatibility response, relay bytes, and retirement receipt.
The relay protocol is unchanged.
Provider setup output stays outside the relay stream; unexpected execution stdout fails the launch with a bounded diagnostic.

A real CLI probe on sbx v0.42.1 preserved 10,254 stdin/stdout bytes including all byte values, separate binary stderr, stdin EOF, and exit status 23, with no terminal processing.
SIGTERM did not end an active exec client within ten seconds.
Killing the exec client's process group ended the transport but left the workload process in the VM.
Consequently, CLI or relay exit is never cleanup evidence: the owner removes the entire microVM.
Cell interrupts still travel through the relay to the worker and retain that VM.

The local owner observes controller input closure, controller loss, signals, and failed execution independently of output backpressure.
Removal uses the provider's documented [`rm --force`](https://docs.docker.com/reference/cli/sbx/rm/) and confirms absence through [`ls --json`](https://docs.docker.com/reference/cli/sbx/ls/).
The owner captures the provider UUID and checks it before removal.
In the verified local CLI, exec and removal take names, not UUIDs; Console uses only its newly allocated name and refuses a name that now identifies a different VM.
Concurrent manual rebinding of that owned name between a listing and a command is outside the CLI's identity contract.
It never adopts an existing sandbox or removes one merely because it shares Console's prefix.

Creation cancellation is tracked from the first create request.
An empty listing after an unacknowledged create is not proof that a service-side operation cannot finish later.
Even removing an observed partial VM does not convert an unacknowledged creation into a confirmed receipt.
An uncertain create, failed removal, unavailable daemon, missing receipt, or identity mismatch blocks replacement and reports the owned name and UUID when known.
The cell is never replayed automatically.
Provider operations and owner retirement have bounded deadlines; the [architecture timing reference](ARCHITECTURE.md#selected-target-sessions-and-timing) records their independent allowances.
Recovery after uncertainty requires inspecting that exact identity through SBX's supported tools.
There is no global prune, reset, daemon termination, adoption, reconnect, or later attachment path.

Forced removal retires the whole microVM, including processes or containers started outside the relay's process group.
Restart creates a new VM from the captured digest.
VM-local filesystem changes are discarded; declared host shares remain.
After host, daemon, or owner-helper failure, Console cannot guarantee independent recovery or eventual removal.
Retain reported identities for manual recovery after the provider becomes available.

Records identify the compute kind, effective provider, CLI version, captured template reference, VM name/UUID, target cwd, and shared-path access separately from controller recording paths.
Target metadata contains no workload environment or credential values.
MicroVM UUIDs are not labeled Docker container IDs.
Quarto exports remain source-only and execute the captured cells when rendered; recreate the runtime and files first.

### Bounded output and retained text

Tool results return bounded text previews with the beginning and latest tail under an 8 KiB total UTF-8 budget; images have separate limits.
A retained-output path is relative to the Console server's recording workspace on the controller.
Reading omitted text requires a filesystem tool with access to that controller directory; access only to the execution target or another client host is insufficient.
Console does not transfer these files or expose a read/search tool.
A log can contain only a retained prefix after the file limit or a write failure; the preview still observes the latest output and reports the loss.
See [the built-in runtime guide](BUILTIN_RUNTIME.md#output-and-notices).

## Acceptance and scope

Real acceptance requires standalone SBX availability and an explicitly prepared template:

```sh
export MCP_CONSOLE_TEST_SBX_TEMPLATE="$(cat console-template-reference.txt)"
scripts/test client_server/server/test_docker_sandbox_runtime \
  client_server/server/test_docker_sandbox_lifecycle \
  client_server/server/test_docker_sandbox_policy
```

Network inheritance acceptance additionally requires `MCP_CONSOLE_TEST_SBX_NETWORK=1` and an existing policy allowing `pypi.org:443` and `registry.npmjs.org:443` while denying `example.com:443`.
Independent container retirement acceptance requires `MCP_CONSOLE_TEST_SBX_INNER_DOCKER=1`, the example `shell-docker` template, and existing provider access to pull `busybox:1.37.0` inside the owned VM.
That test invokes the inner Docker CLI through `sbx exec`; it never uses the controller Docker daemon to control the Sandbox.
Tests modify only rules scoped to their owned VM, verify actual HTTP access, and compare global rules and unrelated resources.
They never initialize or reset the user's policy.
Real fixtures serialize access to the VM runtime because SBX's default per-VM memory allocation can overcommit a controller under CPU-count test concurrency.
Discovery checks provider availability without restricting its version; the adapter enforces the minimum version during execution.
Fake CLI tests run separately and cover the minimum and newer versions, deterministic uncertainty, version/schema failures, argument handling, and replacement barriers; they are not evidence of microVM execution.

The implementation was exercised on macOS 26.6.2 arm64 with sbx v0.42.1 (`cc6e400a4a3ce3ce5e0b2b77b8ee352aac854c64`).
The runtime, lifecycle, and policy suites also passed on macOS 26.7 arm64 with sbx v0.45.1 (`9d79d90ee4c5d297fb3d36b75384e8cea7a4fbcb`), including network-policy and inner-Docker acceptance.
Linux controller acceptance requires a supported SBX/KVM host; Docker Engine on Linux is insufficient evidence.
The same capability-based tests apply without per-test OS allowlists.

Deferred: native policy translation or inner native layering, ordinary Docker compute enforcement, SSH/cloud targets, Windows, managed preparation, automatic template builds, kits, clone/sync, GPU/resource flags, arbitrary provider arguments, reused VMs, and later attachment.
