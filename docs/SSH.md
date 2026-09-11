# SSH execution

`mcp-console serve` can keep the MCP server and recordings local while running its relay and built-in worker on one existing SSH host.
The host needs a compatible Console build, a supported native sandbox environment, and an existing workspace.
R, Python, packages, and SQL adapters must already be installed there.
Console does not synchronize files or prepare remote dependencies.

## Configure a target

For an existing `ssh mule` configuration, first create or select the remote project yourself and confirm that `ssh mule uvx mcp-console --version` works.
Save this in the local project's `.agents/console/config.yaml`:

```yaml
extends: ":workspace"
target:
  transport:
    kind: ssh
    host: mule
  workspace: /srv/projects/analysis
```

Then run `mcp-console serve` from the local project.
The first operation that starts a worker connects to `mule`, checks compatibility and the remote directory, validates the sandbox there, and starts the relay and worker in `/srv/projects/analysis`.
`extends` is optional: omitting it preserves Console's restricted policy with host reads and private temporary writes.
Selecting SSH alone grants no workspace writes.

| Field                   | Default and meaning                                                                                                                                                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `target`                | Omitted: local execution. When present, selects the one SSH target for the implicit session. Applies only to `serve`.                                                                                                                                          |
| `target.transport.kind` | Required; only `ssh` is supported.                                                                                                                                                                                                                             |
| `target.transport.host` | Required, nonempty OpenSSH destination, including a host alias. Uses the controller's SSH configuration for identity, user, port, jump hosts, and host keys.                                                                                                   |
| `target.workspace`      | Required absolute path on the execution host. The helper verifies that it exists and is a directory; it never creates it or substitutes another cwd. Relative or missing values fail locally; inaccessible, nonexistent, or non-directory paths fail remotely. |
| `target.command`        | Optional nonempty string argv; defaults to `[uvx, mcp-console]`. Its first element must name an executable. NUL bytes are rejected. Console quotes each argument for the remote shell and appends the fixed internal `ssh-launch` operation.                   |

For example, `target.command: [uvx, mcp-console==0.0.3]` selects a package version, and `target.command: [/opt/console/bin/mcp-console]` selects a preinstalled build.
The selected package must implement this SSH protocol; a version pin is not a compatibility guarantee.
The command is a trusted executable prefix, not a shell program or a `send` argument.
It must leave stdout exclusively for Console's launch protocol; setup diagnostics belong on stderr.
Unexpected stdout is an error.

`serve --no-sandbox` retains the configured SSH target and remote directory.
It launches the remote relay directly, with the remote account's permissions and the existing direct-worker cleanup limitations.
It still reads configuration to select the target, so malformed YAML is an error.
Sandbox permission fields are not enforced in this mode; target environment controls still apply remotely.
Custom development `--worker` and `--relay` replacements cannot be combined with SSH.

Standalone `mcp-console sandbox -- COMMAND` remains local.
It uses the local cwd for built-ins, relative policy paths, and `--writable-root`, regardless of `target`.

## Policy and preinstalled runtimes

The local server captures the target, selected built-in, native policy adjustments, and repeatable `--writable-root` arguments once.
Every remote launch consumes that structured snapshot without reading remote YAML.
Later local or remote configuration edits cannot change the session's target or selected policy.
The helper applies Console's additions and native preflight on the execution host, including that host's platform defaults.
Relative filesystem entries and CLI writable roots resolve against the fixed remote workspace.
Literal paths retain their existing semantics: no tilde or environment expansion and no symlink canonicalization by Console.
Native modes, proxy fields, omitted values, and explicit nulls retain the [sandbox configuration](SANDBOX_CONFIGURATION.md) behavior and runner validation.

The worker inherits the remote environment, then applies `sandbox.environment` and `sandbox.inherit_environment`.
These settings configure the workload, not SSH, `uvx`, the launch helper, or trusted runner setup.
The controller does not send its ambient R/Python paths, `HOME`, `TMPDIR`, or loader variables.
For an installation outside the remote SSH `PATH`, configure the execution-host paths explicitly:

```yaml
target:
  transport: {kind: ssh, host: mule}
  workspace: /srv/projects/analysis
sandbox:
  environment:
    R_HOME: /opt/R/4.6.1/lib/R
    R_LIBS_USER: /srv/R/library
    RETICULATE_PYTHON: /srv/venvs/analysis/bin/python
```

The remote R installation must include its shared `libR` library and be discoverable through remote `R_HOME` or `PATH`.
Python currently needs the remote `reticulate` R package and a compatible installed Python interpreter.
SQL needs an available R DBI or Python DB-API provider.
The R-backed SQL adapter, including the default DuckDB connection and its result formatter, needs `DBI`, `duckdb`, `nanoarrow`, `arrow`, `tibble`, `pillar`, and `utf8`, with their dependencies.
Python DB-API connections use the selected interpreter and its installed database driver.
Install any packages and DuckDB extensions needed by the analysis before starting the session.
Missing packages and unavailable adapters produce ordinary runtime errors.

SSH always uses Console's bare-runtime capability model, even when remote `uv` is available.
No managed defaults are prepared, the schema omits `requirements`, and supplied preparation requests fail before evaluation, stdin delivery, or restart.
Automatic Console R/Python resolvers are disabled; unexpected callbacks cannot invoke local resolvers.
Console preserves a configured remote `RETICULATE_PYTHON` and disables reticulate's default managed-venv selection.
The `managed` Python selector is unsupported in this mode.
The internal `MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION`, `MCP_CONSOLE_MANAGED_PYTHON`, `MCP_CONSOLE_PREINSTALLED`, and `RETICULATE_USE_MANAGED_VENV` capability settings cannot be overridden by worker environment configuration.
User-written installation code remains subject to the chosen sandbox policy.

A configured sandbox proxy runs on the remote host; its host-local addresses refer to that host.
SSH transport and package bootstrap run outside the worker's network policy.

## Lifecycle and records

The system OpenSSH client uses batch mode, no PTY, and no agent forwarding.
Host-key verification remains under the user's OpenSSH configuration.
Console can use an existing shared connection but never manages or terminates its master.
Authentication, executable lookup, bootstrap, and native setup diagnostics remain visible on stderr.
The connection timeout is 10 seconds; the separate Console startup deadline is 30 seconds, including package bootstrap, preflight, and worker readiness.
Neither is controlled by `send.timeout_ms`, and session shutdown cancels an outstanding startup.

Evaluation, polling, stdin, output, images, interrupts, shutdown, and replacement use the existing relay protocol and generation rules.
The remote helper observes connection closure independently of output backpressure and requests ordinary runner retirement, including before worker readiness.
Local shutdown retains the existing staged bound of approximately 10 seconds, including forced local SSH termination if needed.
The local SSH process exiting is not proof that remote cleanup completed.
On a healthy connection, the helper acknowledges retirement after the runner exits successfully and queued output is forwarded.
Without that acknowledgment, Console reports unconfirmed retirement and prevents further replacement in the session.
Cells are never replayed after transport failure.

There is no reconnect, resume, heartbeat, or lease protocol.
During an undetected network partition, remote cleanup may be delayed until SSH detects the connection loss.
Client-side SSH keepalives do not establish bounded remote retirement.
The runner retains its own limits, including no independent recovery after runner death.
Direct execution retains its lack of runner-owned descendant cleanup.

Journals, output spools, transcripts, and returned image bytes stay in the local project's `.agents/console/sessions/`.
Session metadata records the SSH destination and initial remote execution directory separately from the local recording workspace.
Arbitrary files created by cells remain remote.
The source-only Quarto projection includes remote target context, omits the controller `root.dir`, and defaults to `execute.eval: false`.
Enable execution only after deliberately preparing an environment and filesystem for those cells; local rendering does not reproduce the remote filesystem.
