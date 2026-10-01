# SSH execution

SSH keeps the MCP server and [recordings](RECORDING.md) on the controller while running the relay and worker on an existing host.
Configuration is captured locally; runtime discovery, dependency preparation, native policy, and user files belong to the remote host.
Console does not install R, create the workspace, or synchronize files.

## Configure a target

Prepare a compatible Console installation and an existing remote directory, then save this in the controller's `.agents/console/config.yaml`:

```yaml
extends: :workspace
target:
  transport: {kind: ssh, host: analysis-host}
  workspace: /srv/projects/analysis
```

Run `mcp-console serve` through an MCP client.
Discovery, preparation, the default worker, and enabled R/Python initialization start in the background; MCP initialization and tool discovery do not wait for them.
Early calls follow the [shared readiness rules](SEND_OPERATIONS.md#server-readiness).
Omitting `extends` retains host reads and private temporary writes, not workspace write access.

| Field                   | Meaning                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `target.transport.host` | Required OpenSSH destination or configured alias.                    |
| `target.workspace`      | Required absolute existing directory on the execution host.          |
| `target.command`        | Optional nonempty argv prefix for a compatible remote Console build. |
| `target.compute`        | Omitted or `{kind: host}`. SSH with Docker or SBX is unsupported.    |

Without `command`, remote lookup tries `mcp-console`, then `uvx mcp-console` only if Console is absent.
A selected command that fails does not trigger fallback.
For a development build, use an explicit path such as `command: [/opt/console/bin/mcp-console]`.
Console quotes arguments for the remote shell; this field is an executable prefix, not shell source.
Its stdout must contain only the private protocol; diagnostics go to stderr.

The system OpenSSH client uses batch mode, no PTY, and no agent forwarding.
User SSH configuration supplies identities, ports, jump hosts, and host-key verification.
Console may use an existing shared connection but never owns or terminates its master.
Custom `--worker` and `--relay` replacements cannot be combined with remote targets.

## Runtime selection and policy

The host needs R for R cells, or CPython with a usable embedding library for Python-only execution.
Managed dependencies require the [resolver prerequisites](REQUIREMENTS.md); without R, managed Python requires remote `uv` on `PATH`.
An invalid explicit R selection or broken discovered installation is an error, not permission to fall back to Python.

Use top-level `python` to select an existing remote environment:

```yaml
python: /srv/venvs/analysis/bin/python
target:
  transport: {kind: ssh, host: analysis-host}
  workspace: /srv/projects/analysis
sandbox:
  environment:
    R_HOME: /opt/R/4.6.1/lib/R
```

Relative Python paths resolve against `target.workspace`.
The `python` setting takes precedence over legacy `sandbox.environment.RETICULATE_PYTHON`.
An explicit Python environment disables managed Python preparation; with R present, managed R and its DuckDB extensions remain available.

The helper materializes the captured native policy on the execution host and never reads remote YAML.
Built-ins, relative filesystem entries, `--writable-root`, private storage, and proxy addresses use the remote workspace and platform.
Configuration changes do not affect an existing session.

Workload `environment` and `inherit_environment` settings do not configure SSH, command bootstrap, or trusted preparation.
Only explicit R/Python selections are conveyed separately to preparation.
The controller does not forward its ambient interpreter paths, home, temporary directory, or loader settings.
The selected runtime configuration takes precedence over conflicting workload variables, including when inheritance is disabled.

`serve --no-sandbox` keeps SSH placement and workload environment controls but removes inner native enforcement and its descendant-cleanup guarantee.
Standalone `sandbox -- COMMAND` remains local.

## Trusted preparation

A separate authenticated connection owns discovery and dependency resolution outside the worker sandbox.
It captures the remote resolver environment once and remains available independently of worker generations.
The controller owns manifests, candidate acceptance, activation, and recording; the remote preparation owner owns subprocesses, caches, and cleanup.
See [requirements transactions](REQUIREMENTS.md#failure-atomicity-and-cache-effects) and [resolver trust](REQUIREMENTS.md#host-resolution-and-trust).

Private requests contain validated requirements and runtime selections, not cells or arbitrary shell programs.
Results can commit only after a compatible response and confirmed resolver cleanup.
A normal installation failure preserves the accepted environment and current worker.
A missing or malformed result, transport loss, or uncertain cleanup blocks further preparation and replacement; the operation is not replayed.
The [target envelope](RELAY_PROTOCOL.md#target-launch-envelope) owns version negotiation and framing.
Launch protocol version 9 carries the controller’s enabled-language selection and requires built-in interpreter-bootstrap completion after transport readiness; older executables are rejected before evaluation even when package versions match.
Preparation protocol version 5 is unchanged.

Preparation is trusted host execution, not a secure isolation boundary.
Package builds and startup code can run with the remote account's permissions, independently of the worker's network policy.
Captured paths do not freeze worker-modifiable files they name.

## Lifecycle and records

SSH connection setup has a 10-second connection timeout; preparation handshake and worker bootstrap each have a separate 30-second setup deadline.
Discovery after the handshake and dependency installation have no such deadline.
`send.timeout_ms` is an observation budget, not resolver cancellation.
Interrupt targets the active preparation operation; input closure cancels setup and preparation even when protocol output is blocked.

Retirement requires the remote helper's acknowledgment after target cleanup and queued output drainage.
Local SSH exit or EOF does not establish remote cleanup.
Without the receipt, Console reports uncertainty and blocks replacement.
Cells are never replayed after transport failure.
There is no reconnect, resume, heartbeat, or lease: an undetected network partition can delay cleanup, and keepalives do not establish a bounded retirement guarantee.
Native runner and direct-launch limitations still apply.

Restart discards remote interpreter state and private temporary storage while preserving accepted environments and resolver caches.
Arbitrary files remain remote.
Returned image bytes and retained output logs are controller files, so reading omitted output requires controller filesystem access.
Quarto exports contain target context and source, not a replica of the remote environment or filesystem.

## Validation

Use the localhost OpenSSH fixtures for ordinary transport coverage and explicit external-host capabilities for cross-host acceptance; see [boundary tests](../tests/boundaries/README.md).
Fake peers establish protocol behavior, not real remote cleanup.
An unavailable remote host is a skip, not a passing execution result.
EOF, cancellation, replacement, and resolver-failure tests must check the cleanup receipt rather than only the local SSH process.
