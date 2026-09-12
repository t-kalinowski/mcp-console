# SSH execution

`mcp-console serve` can keep the MCP server and recordings local while running its relay and built-in worker on one existing SSH host.
The host needs a compatible Console build, a supported native sandbox environment, and an existing workspace.
The host needs a working R installation, `uv` (or another supported resolver bootstrap), and system libraries and build tools required by the requested packages.
Console prepares its managed R, Python, and DuckDB environments there.
It does not install R or synchronize files.

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
Before advertising MCP tools, Console connects to `mule`, checks compatibility and the remote directory, and discovers that host's resolver capability.
This does not install analysis packages or start a worker.
The first operation that needs an environment prepares the managed defaults there; worker launch then validates the sandbox and starts the relay and worker in `/srv/projects/analysis`.
`extends` is optional: omitting it preserves Console's restricted policy with host reads and private temporary writes.
Selecting SSH alone grants no workspace writes.

| Field                   | Default and meaning                                                                                                                                                                                                                                            |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `target`                | Omitted: local execution. When present, selects the one SSH target for the implicit session. Applies only to `serve`.                                                                                                                                          |
| `target.transport.kind` | Required; only `ssh` is supported.                                                                                                                                                                                                                             |
| `target.transport.host` | Required, nonempty OpenSSH destination, including a host alias. Uses the controller's SSH configuration for identity, user, port, jump hosts, and host keys.                                                                                                   |
| `target.workspace`      | Required absolute path on the execution host. The helper verifies that it exists and is a directory; it never creates it or substitutes another cwd. Relative or missing values fail locally; inaccessible, nonexistent, or non-directory paths fail remotely. |
| `target.command`        | Optional nonempty string argv; defaults to `[uvx, mcp-console]`. Its first element must name an executable. NUL bytes are rejected. Console quotes each argument for the remote shell and appends a fixed internal launch or preparation operation.            |

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

## Runtime selection and policy

The local server captures the target, selected built-in, native policy adjustments, and repeatable `--writable-root` arguments once.
Every remote launch consumes that structured snapshot without reading remote YAML.
Later local or remote configuration edits cannot change the session's target or selected policy.
The helper applies Console's additions and native preflight on the execution host, including that host's platform defaults.
Relative filesystem entries and CLI writable roots resolve against the fixed remote workspace.
Literal paths retain their existing semantics: no tilde or environment expansion and no symlink canonicalization by Console.
Native modes, proxy fields, omitted values, and explicit nulls retain the [sandbox configuration](SANDBOX_CONFIGURATION.md) behavior and runner validation.

The worker inherits the remote environment, then applies `sandbox.environment` and `sandbox.inherit_environment`.
These settings configure the workload, not SSH, `uvx`, or trusted preparation.
Two runtime selections also inform preparation: an explicit `sandbox.environment.R_HOME` selects the remote R installation, and `sandbox.environment.RETICULATE_PYTHON` selects the remote Python mode.
The controller extracts only string selections; malformed environment fields remain in the captured policy for validation on the execution host.
No other workload environment settings are applied to the preparation owner.
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
Managed mode prepares the same [defaults and additions](REQUIREMENTS.md) as local execution, including reticulate and SQL adapters.
An explicit Python path disables managed Python additions and automatic Python imports, while managed R and DuckDB remain available.
Omitting that selection, using an empty value, or selecting `managed` retains managed Python when a bootstrap is available.
The selected interpreter must already exist remotely and contain the Python packages needed by the analysis.

The trusted preparation owner captures the execution host's environment once, including the R installation, `ir`/`uv` selection, Python preference, package-source settings, inherited R library paths, and cache locations.
Later worker mutations cannot change these choices.
The worker receives the discovered R home, resolved managed R libraries, Python selection, and capability flags with precedence over conflicting workload overrides, including with `inherit_environment: false`.
R libraries and Python executables are validated on the remote host; their paths are only metadata on the controller.
Other workload settings retain their existing meaning.
In particular, configuring a workload cache does not relocate trusted preparation caches.

When discovery finds no resolver bootstrap, Console retains the bare-runtime model: the schema omits `requirements`, automatic resolution is disabled, and available preinstalled packages and adapters can still be used.
A selected bootstrap that fails later reports an error; it does not change the schema, select a different bootstrap, or run a controller resolver.
Bare and user-selected Python modes disable reticulate's implicit managed-venv installation.
Managed Python uses the existing server callbacks and retained manifest; the worker stays offline and does not install its own environment.

## Trusted preparation

A separate authenticated SSH connection runs a private preparation owner outside the worker sandbox.
It remains available without a relay or worker, including for discovery and standalone `send(requirements=...)`.
Its operations are limited to bootstrap preparation, R libraries, Python manifests and version selection, and DuckDB extensions.
They call the same embedded resolver implementation used locally.
The private requests carry no shell programs, source code, executable choices, or arbitrary environment overrides.
Preparation uses a separate versioned, length-prefixed JSON protocol with a 1 MiB message limit; installer output is captured separately from protocol frames.
Oversized preparation requests are rejected before remote admission and leave the session available for subsequent requests.
Large results and installer errors use bounded result chunks followed by the cleanup receipt, preserving the complete result without changing its failure classification.
Launch protocol version 2 carries the selected environment, and preparation protocol version 3 requires a matching Console package version.
Older preinstalled-only peers fail compatibility checks before MCP readiness.
Resolver programs, temporary files, interpreter checks, Matplotlib preparation, and caches belong to the execution host.
Python resolution retains the existing treatment of `UV_OFFLINE`, `UV_NO_CACHE`, and `RETICULATE_UV`.

The local server owns admitted operations, requirement merging, candidate and retained environments, activation receipts, and worker generations.
The preparation owner retains trusted resolver configuration, not session manifests or activation decisions.
Each operation runs its own resolver process groups and reports an explicit result and cleanup status before the server can commit a candidate.
An interrupt accepted between resolver stages remains owned by that preparation operation and applies to its next resolver.
An ordinary installation failure with confirmed cleanup retains the existing transaction behavior, including preservation of a healthy old worker during failed restart preparation.
Missing, malformed, or truncated results, failed cleanup, and detected transport loss prevent further preparation and worker replacement in that session.
Preparation is never automatically replayed after uncertain completion.

Accepted requirements retain the [existing trust boundary](REQUIREMENTS.md#host-resolution-and-trust): installation and build code may execute with the remote account's trusted setup permissions.
Preparation has the remote account's network access, independently of workload restrictions.
Automatic R requests still accept only plain package names, explicit R references remain subject to `IR_NO_LOCAL_SOURCES`, and Python requirements, version constraints, and DuckDB extension names retain their validators.

A configured sandbox proxy runs on the remote host; its host-local addresses refer to that host.
SSH transport and package bootstrap run outside the worker's network policy.

## Lifecycle and records

The system OpenSSH client uses batch mode, no PTY, and no agent forwarding.
Host-key verification remains under the user's OpenSSH configuration.
Console can use an existing shared connection but never manages or terminates its master.
Authentication, executable lookup, bootstrap, and native setup diagnostics remain visible on stderr.
The connection timeout is 10 seconds.
Discovery and each worker launch have a separate 30-second setup deadline, including command-prefix bootstrap and, for worker launch, preflight and readiness.
Dependency installation has no 30-second deadline.
An ordinary cell may return running while defaults or automatic dependencies resolve; explicit preparation retains its ordering and wait semantics.
`send.timeout_ms` never cancels a resolver.
Interrupt and cancellation messages identify the preparation operation and reach its remote resolver process group, independently of the worker connection.
Closing MCP input cancels discovery before readiness and active preparation during shutdown.
The remote preparation owner observes input closure independently of blocked protocol output and cancels and reaps its resolver groups.

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
