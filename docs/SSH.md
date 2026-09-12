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

`target.lease_ms` sets each channel's controller lease in integer milliseconds, from 1,000 through 300,000 (default 30,000).
Heartbeat challenges are spaced at one sixth of that lease (5 seconds by default).
The server freezes this setting with the target.
Select a lease that allows for host scheduling and SSH latency; it does not extend connection or setup deadlines.

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
Missing, malformed, or truncated results and failed cleanup prevent further preparation and worker replacement in that session.
A brief transport interruption can recover the original result from the same owner; it never reruns a resolver to discover whether installation succeeded.
An unrecoverable interruption retains the uncertainty barrier.

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
Both SSH channels have independent leases, including during discovery before a worker exists.
A healthy preparation channel cannot renew the worker channel, or the reverse.

Evaluation, polling, stdin, output, images, interrupts, shutdown, and replacement use the existing relay protocol and generation rules.
The remote session owner survives loss of an individual SSH attachment until its lease expires.
Its launch helper remains the sandbox runner's caller throughout recovery, including before worker readiness.
Local shutdown retains the existing staged bound of approximately 10 seconds, including forced local SSH termination if needed.
The local SSH process exiting is not proof that remote cleanup completed.
On a healthy connection, the helper acknowledges retirement after the runner exits successfully and queued output is forwarded.
Without that acknowledgment, Console reports unconfirmed retirement and prevents further replacement in the session.
Recovery retransmits missing transport data to the same surviving owner; it does not resubmit an evaluation to a new worker.

Each remote channel issues an unpredictable challenge and requires its response before starting discovery, preflight, or other work.
It issues the next challenge only after accepting the previous response and waiting the heartbeat interval.
Only a matching, outstanding response renews its monotonic deadline; writes, socket connectivity, and unsolicited or repeated heartbeats do not.
If either communication direction stalls, the channel expires even when SSH never reports EOF.
Expiry is terminal and closes the existing owner's input to request retirement.
The launch owner retires the ordinary sandbox launcher; the preparation owner stops and reaps its existing resolver groups.
Idle sessions, busy cells, prompts, long installations, partial frames, and output backpressure remain subject to that deadline.
The deadline bounds the retirement request, not completion of native cleanup.

### Recovery within one live local session

The two channels recover independently, including preparation before a worker exists.
A local adapter detects EOF or an I/O failure immediately; two missing heartbeat intervals also cause it to replace an otherwise stalled SSH attachment.
It retries with delays of 100, 200, 400, 800, then at most 1,000 milliseconds, bounded by its conservative lease deadline.
Each recovery attempt uses the ordinary 30-second setup budget, capped by the remaining lease.
This includes OpenSSH and command-prefix startup; once connected, an owner bounds each authentication exchange to two seconds without pausing its lease.
Authentication attempts do not renew the remote lease.
A definite remote command failure, such as a missing executable, stops recovery and retains uncertainty about any prior ownership.

The controller assigns each owner a random identity, channel, worker generation, and private capability before its first creation attempt.
It requests creation only once.
If a bootstrap or Hello reply is lost, subsequent attempts may only attach to that identity; a missing owner is an explicit uncertainty error.
They never repeat creation, recapture resolver settings, reread YAML, or start a fresh worker.
The same local MCP server and its adapters must remain alive.
A stopped adapter loses its recovery state; another or restarted server cannot attach.
There is no permanent service, global session registry, saved credential, or later-attachment interface.

Transport states distinguish a connected attachment, recovery in progress, requested retirement, and terminal failure.
During recovery, existing waits and polls retain their output and operation identity; MCP remains responsive.
New cells, requirements, and restart requests are rejected while either channel reports recovery, with an explicit message that the work was not admitted.
Already admitted evaluations, stdin, interrupts, activation replies, restart retirement, and preparation operations retain their ordered stream identities.
They resume with that owner and generation or fail under the existing lifecycle bounds.
A healthy channel does not clear uncertainty about the other.

A successful attachment proves identity and resumes stream cursors; it cannot clear a missing cleanup receipt.
The launch helper's original terminal receipt and preparation's confirmed completion results remain authoritative.
Prepared environments still require the existing generation and activation checks before commit, including candidate preparation before old-worker retirement during restart.
An ordinary installation failure retains its existing transaction behavior.
Missing owners, host reboot, expiry, incompatible builds, protocol corruption, or lost retention cause terminal failure without a fresh session or automatic resubmission.
An operation may already have had effects when its owner disappears.
Console guarantees at-most-once dispatch while the same owner retains the necessary state, not exactly-once external effects or rollback of package-cache mutations.

Explicit shutdown retains the existing protocol request and does not wait for lease expiry while communication is available.
Controller input closure stops recovery attempts and requests retirement through an available attachment.
The remote owner cancels queued input when it receives that closure, even if the helper is not reading, and uses a separate eight-second retirement deadline.
Continuing heartbeats cannot extend shutdown.
When communication is unavailable, local shutdown remains bounded and remote lease expiry is the fallback.
Expiry closes the helper input to initiate cleanup; elapsed time never proves that cleanup succeeded.
Without an explicit valid launch or preparation receipt, retirement stays unconfirmed and conflicting preparation or replacement remains blocked.
The runner retains its limits, including no independent recovery after runner death.
Direct execution retains its lack of runner-owned descendant cleanup.

### Ownership and attachment authentication

Each server-owned `ssh-connect` adapter runs disposable OpenSSH connections and remote `ssh-tunnel` forwarding processes.
The first forwarding process creates a separate `ssh-owner` with a private Unix socket in a randomly named directory under `/tmp`.
That owner holds the existing `ssh-launch` or `ssh-prepare` child, replay state, and monotonic lease across attachments.
It leaves no listening network service.
Normal terminal exit removes the socket and directory; a killed owner can leave an unusable directory, which never authorizes recreation or attachment.

The directory has mode 0700, but same-UID permissions alone do not isolate it from a sandboxed workload.
A 256-bit capability is generated locally and sent only through the first encrypted SSH bootstrap and a private owner startup pipe.
It remains in owner and adapter memory and is absent from arguments, workload environment, files, logs, and recordings.
The default sandbox's process and descriptor isolation prevents workload access to that trusted memory and its startup pipes.
Deliberately unrestricted execution cannot provide this isolation from other processes of the same account.

Attachments mutually authenticate with HMAC-SHA256 over fresh nonces and the complete immutable identity, channel, generation, lease, build, and requested epoch.
They derive an attachment key and authenticate every subsequent frame, including its direction, epoch, serial, tag, and payload.
This prevents a substituted Unix socket from stealing the capability or injecting commands after forwarding a valid handshake.
Only an authenticated higher epoch can replace the current stream.
The owner checks that epoch at installation, so simultaneous attempts cannot install two current attachments.
Failed, stale, and partial handshakes neither displace a live attachment nor renew its lease.
At most four bounded handshake candidates run alongside the owner; workload output cannot block authentication or expiry processing.

### Private recovery envelope

Recovery protocol 2 checks the Console package version, channel, generation, identity, and frozen lease before starting the helper.
It wraps both directions; its controls never enter relay JSONL or the typed preparation protocol.
Every frame has a one-byte tag, a four-byte big-endian payload length, and at most 32 KiB of payload.
Initial request/challenge messages are structured JSON; nonce and capability values have 32 random bytes.
Authenticated frames carry an eight-byte monotonically increasing attachment serial and a 32-byte HMAC around their body.
The tags are Hello (1), data (2), ingestion acknowledgment (3), challenge (4), response (5), stream end (6), end acknowledgment (7), authentication proof (8), resume cursor (9), and terminal failure (10).

A command is identified by its position and content in its owner's immutable input stream; packet boundaries can split or combine inner messages.
Each data packet has a stable eight-byte stream ID, the preceding 32-byte SHA-256 chain anchor, a one-byte stream kind, and at most 16 KiB of data.
Kind 0 carries the application stream; kind 1 carries remote diagnostics to local stderr.
The next anchor hashes the preceding anchor, ID, kind, and exact bytes.
Repeated delivery of a still-pending packet must match its content and never writes those bytes twice.
Stale IDs outside retained history and IDs reused with different content are errors.

Acknowledgments and resume/end cursors carry the last fully ingested packet's ID and chain anchor.
Ingestion means that its bytes have been written to the receiving application pipe, independently of MCP response delivery.
Local adapter records distinguish stream fragments from connection-status updates; decoding them preserves incomplete inner frames.
On attachment replacement, both ends verify the peer's ingestion cursor, retain partially ingested packets, and resend only the unacknowledged suffix.
The existing relay/preparation parsers survive, so evaluations, interactive input, control IDs, results, images, and terminal receipts are not dispatched or delivered twice.
A worker or resolver that finishes just before disconnection can return its retained original terminal outcome.

Each direction retains at most 256 KiB and 128 unacknowledged data packets.
Larger outputs and results stream through that window; they need not fit in one replay message.
Capacity applies backpressure to application and diagnostic reads while the readiness loop continues to service control, cancellation, partial frames, and monotonic deadlines.
Authenticated input exceeding the receive window or bounded control capacity fails explicitly.
Acknowledged payloads are removed.
Each receiver retains at most 128 content-hash receipts to recognize replay that crosses an acknowledgment while local ingestion advances; older stale IDs fail.
The terminal handshake is repeated on the current attachment, after its replay data, before closing local output.
A helper can close input before its final output is drained; late input is left unacknowledged while that original terminal result is preserved.
If the local server cancels its output reader, the adapter requests retirement and abandons delivery without acknowledging the discarded bytes or claiming cleanup.
The end handshake drains remote output before local closure; controller input closure requests prompt cancellation even when application input is backpressured.
Native cleanup and the existing inner receipts retain their separate ownership and meaning.

Journals, output spools, transcripts, and returned image bytes stay in the local project's `.agents/console/sessions/`.
Session metadata records the SSH destination and initial remote execution directory separately from the local recording workspace.
Arbitrary files created by cells remain remote.
The source-only Quarto projection includes remote target context, omits the controller `root.dir`, and defaults to `execute.eval: false`.
Enable execution only after deliberately preparing an environment and filesystem for those cells; local rendering does not reproduce the remote filesystem.
