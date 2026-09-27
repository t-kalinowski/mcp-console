# SSH execution

`mcp-console serve` can keep the MCP server and recordings local while running its relay and built-in worker on one existing SSH host.
The host needs a compatible Console build, a supported native sandbox environment, and an existing workspace.
The host needs a working R installation, `uv` (or another supported resolver bootstrap), and system libraries and build tools required by the requested packages.
Console prepares its managed R, Python, and DuckDB environments there.
It does not install R or synchronize files.

## Configure a target

For an existing `ssh analysis-host` configuration, first create or select the remote project yourself.
Save this in the local project's `.agents/console/config.yaml`:

```yaml
extends: :workspace
target:
  transport:
    kind: ssh
    host: analysis-host
  workspace: /srv/projects/analysis
```

Then run `mcp-console serve` from the local project.
Before advertising MCP tools, Console connects to `analysis-host`, checks protocol compatibility and the remote directory, and discovers that host's resolver capability.
This does not install analysis packages or start a worker.
The first operation that needs an environment prepares the managed defaults there; worker launch then validates the sandbox and starts the relay and worker in `/srv/projects/analysis`.
`extends` is optional: omitting it preserves Console's restricted policy with host reads and private temporary writes.
Selecting SSH alone grants no workspace writes.

| Field                   | Default and meaning                                                                                                                                                                                                                                                   |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `target`                | Omitted: local host execution. With SSH transport, selects one host for the implicit session. Applies only to `serve`.                                                                                                                                                |
| `target.transport.kind` | Select `ssh` for this host target; Docker and Docker Sandbox separately accept local transport.                                                                                                                                                                       |
| `target.transport.host` | Required, nonempty OpenSSH destination, including a host alias. Uses the controller's SSH configuration for identity, user, port, jump hosts, and host keys.                                                                                                          |
| `target.workspace`      | Required absolute path on the execution host. The helper verifies that it exists and is a directory; it never creates it or substitutes another cwd. Relative or missing values fail locally; inaccessible, nonexistent, or non-directory paths fail remotely.        |
| `target.command`        | Optional nonempty string argv. When omitted, runs `mcp-console` from the remote `PATH`, or `uvx mcp-console` if it is absent. Console quotes each argument for the remote shell and appends a fixed internal launch or preparation operation. NUL bytes are rejected. |

`target.compute` is optional for SSH and accepts `{kind: host}`; SSH plus Docker or Docker Sandbox is unsupported.

For example, `target.command: [uvx, mcp-console==0.0.3]` selects a package version, and `target.command: [/opt/console/bin/mcp-console]` selects a preinstalled build.
The selected package must implement this SSH protocol; a version pin is not a compatibility guarantee.
Console does not preflight the selected executable or command prefix.
Missing commands, installation failures, and command errors propagate through ordinary startup failure handling.
An installed `mcp-console` that fails does not trigger the `uvx` fallback.
The command is a trusted executable prefix, not a shell program or a `send` argument.
It must leave stdout exclusively for Console's launch protocol; setup diagnostics belong on stderr.
Unexpected stdout is an error.

`serve --no-sandbox` retains the configured SSH target and remote directory.
It launches the remote relay directly, with the remote account's permissions and the existing direct-worker cleanup limitations.
It still reads configuration to select the target, so malformed YAML is an error.
Sandbox permission fields are not enforced in this mode; target environment controls still apply remotely.
Custom development `--worker` and `--relay` replacements cannot be combined with SSH, Docker, or Docker Sandbox.

Standalone `mcp-console sandbox -- COMMAND` remains local for supported native selections and rejects resolved compute enforcement.
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
Two runtime selections also inform preparation: an explicit `sandbox.environment.R_HOME` selects the remote R installation, and `sandbox.environment.RETICULATE_PYTHON` remains an explicit Python selection for compatibility.
The top-level `python` setting selects an existing remote interpreter and takes precedence over that compatibility setting.
Relative `python` paths resolve against `target.workspace` on the execution host; the controller does not inspect or resolve them.
The controller extracts only string selections; malformed environment fields remain in the captured policy for validation on the execution host.
No other workload environment settings are applied to the preparation owner.
The controller does not send its ambient R/Python paths, `HOME`, `TMPDIR`, or loader variables.
For an installation outside the remote SSH `PATH`, configure the execution-host paths explicitly:

```yaml
target:
  transport: {kind: ssh, host: analysis-host}
  workspace: /srv/projects/analysis
sandbox:
  environment:
    R_HOME: /opt/R/4.6.1/lib/R
    R_LIBS_USER: /srv/R/library
    RETICULATE_PYTHON: /srv/venvs/analysis/bin/python
```

The preparation owner discovers R from remote `R_HOME` or `PATH`.
An invalid explicit R selection or broken discovered installation reports an R error.
With R present, managed preparation retains the existing reticulate and SQL adapters and their [defaults and additions](REQUIREMENTS.md).
An explicit Python path disables managed Python additions and automatic Python imports, while managed R and DuckDB remain available.

When R is absent, Console starts the native Python and SQL runtime.
With no explicit `python`, remote uv prepares NumPy, pandas, and DuckDB before the first worker starts; missing uv and resolution failures are reported without selecting a PATH interpreter instead.
The native worker uses its Python and DB-API adapters without starting R, reticulate, or R DBI.
An explicit `python` path bypasses uv and uses packages, DuckDB, and custom DB-API connections already available in that environment.
R cells and R requirements are unavailable.

The trusted preparation owner captures the execution host's environment once, including the applicable `ir` or `uv` selection, Python preference, package-source settings, and cache locations.
Later worker mutations cannot change these choices.
The worker receives the discovered R home or inspected native Python selection, resolved managed environments, extension-cache path, and capability flags with precedence over conflicting workload overrides, including with `inherit_environment: false`.
R libraries and Python executables are validated on the remote host; their paths are only metadata on the controller.
Other workload settings retain their existing meaning.
In particular, configuring a workload cache does not relocate trusted preparation caches.

When R is present but discovery finds no resolver bootstrap, Console retains the bare-runtime model: the schema exposes only `requirements.action="get"`, automatic resolution is disabled, and available preinstalled packages and adapters can still be used.
A selected bootstrap that fails later reports an error; it does not change the schema, select a different bootstrap, or run a controller resolver.
Bare and user-selected Python modes disable reticulate's implicit managed-venv installation when R is present.
Managed Python uses the existing server callbacks and retained manifest; the worker stays offline and does not install its own environment.
For native Python, the accepted manifest, managed environment, and inspected launch configuration commit together after preparation and activation.
Plain restart and crash replacement use that accepted selection.

## Trusted preparation

A separate authenticated SSH connection runs a private preparation owner outside the worker sandbox.
It remains available without a relay or worker, including for discovery and standalone `send(requirements=...)`.
Its operations are limited to runtime discovery, bootstrap preparation, R libraries, Python manifests and version selection, selected-interpreter inspection, and DuckDB extensions.
They call the same embedded resolver implementation used locally.
The private requests carry no submitted cells, shell programs, or arbitrary environment overrides.
They carry the configured interpreter selection, and live native preparation carries the running interpreter path as a constraint.
Preparation uses a separate versioned, length-prefixed JSON protocol with a 1 MiB message limit; installer output is captured separately from protocol frames.
Oversized preparation requests are rejected before remote admission and leave the session available for subsequent requests.
Large results and installer errors use bounded result chunks followed by the cleanup receipt, preserving the complete result without changing its failure classification.
SSH launch and preparation protocol version 4 carry the optional native selection and inspection operations.
R-present payloads omit native fields when unused.
Docker and Docker Sandbox retain their existing launch version.
Both require a matching Console package version.
The optional `selected_python` field constrains live managed Python preparation to the running executable, locally or over SSH.
Older peers fail compatibility checks before MCP readiness or worker startup.
Resolver programs, temporary files, interpreter checks, Matplotlib preparation, and caches belong to the execution host.
Python resolution retains the existing treatment of `UV_OFFLINE` and `UV_NO_CACHE`.
Sans-R preparation selects remote uv from startup `PATH` and ignores `RETICULATE_UV`; R-present selection retains its existing behavior.

The local server owns admitted operations, requirement merging, candidate and retained environments, activation receipts, and worker generations.
The preparation owner retains trusted resolver configuration, not session manifests or activation decisions.
Each operation runs its own resolver process groups and reports an explicit result and cleanup status before the server can commit a candidate.
An interrupt accepted between resolver stages remains owned by that preparation operation and applies to its next resolver.
An ordinary installation or inspection failure with confirmed cleanup preserves the current worker, Python objects, SQL catalog, and committed declaration before retirement.
Missing, malformed, or truncated results, failed cleanup, and detected transport loss prevent further preparation and worker replacement in that session.
Preparation is never automatically replayed after uncertain completion.
An accepted live activation remains committed after a later cell or import error.
Activation failures retain their diagnostics and can require restart; Console does not replay the cell or replace the worker to recover from preparation.

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
Direct sans-R launches use a remote private temporary directory retained until the relay retires.
Native sandbox launches use runner-owned private storage.
Neither retirement path removes accepted Python environments or shared uv and DuckDB extension caches.
Python-backed DuckDB extension preparation uses the extension-cache path captured from an absolute remote startup `HOME`; later worker changes to `HOME` or workload cache settings do not redirect preparation or loading.
The remote helper observes connection closure independently of output backpressure and requests ordinary runner retirement, including before worker readiness.
Local shutdown retains the existing staged bound of approximately 10 seconds, including forced local SSH termination if needed.
The [architecture timing reference](ARCHITECTURE.md#selected-target-sessions-and-timing) records the separately owned setup and retirement allowances.
The local SSH process exiting is not proof that remote cleanup completed.
On a healthy connection, the helper acknowledges retirement after the runner exits successfully and queued output is forwarded.
Without that acknowledgment, Console reports unconfirmed retirement and prevents further replacement in the session.
Cells are never replayed after transport failure.

There is no reconnect, resume, heartbeat, or lease protocol.
During an undetected network partition, remote cleanup may be delayed until SSH detects the connection loss.
Client-side SSH keepalives do not establish bounded remote retirement.
The runner retains its own limits, including no independent recovery after runner death.
Direct execution retains its lack of runner-owned descendant cleanup.

Journals, output spools, transcripts, and returned image bytes stay in the local project's `.agents/console/sessions/` when `.agents/console` already exists there, or in the controller's `~/.agents/console/sessions/` otherwise.
The controller's `MCP_CONSOLE_HOME` can replace the fallback directory without changing the remote account's home or configuration.
Session metadata records the SSH destination and initial remote execution directory separately from the controller's recording workspace.
Paths returned by remote Python and SQL cells name execution-host files; recordings and image artifacts remain on the controller.
Arbitrary files created by cells remain remote.
The source-only Quarto projection includes remote target context and omits the controller `root.dir`.
Rendering executes the captured cells, so prepare an appropriate environment and files first; local rendering does not reproduce the remote filesystem.

### Bounded output and retained text

Tool results return bounded text previews with the beginning and latest tail under an 8 KiB total UTF-8 budget; images have separate limits.
A retained-output path is relative to the controller's launch directory for project recordings and absolute for home recordings.
Reading omitted text requires a filesystem tool with access to the selected controller directory; access only to the execution target or another client host is insufficient.
Console does not transfer these files or expose a read/search tool.
A log can contain only a retained prefix after the file limit or a write failure; the preview still observes the latest output and reports the loss.
See [the built-in runtime guide](BUILTIN_RUNTIME.md#output-and-notices).
