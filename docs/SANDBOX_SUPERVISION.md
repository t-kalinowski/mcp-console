# macOS sandbox supervision

**Status:** Current implementation

This document describes the `mcp-console sandbox` launcher's host-side lifetime ownership for sandboxed worker generations and direct commands.
`mcp-console serve --no-sandbox` bypasses this launcher and its manager; it supplies no sandbox policy, sandbox-owned private temporary directory, or descendant-cleanup guarantee.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Lifetime ownership

Each sandbox lifetime has one host-side manager outside Seatbelt and one private sandbox executable as its direct root and process-group leader.
The runner initially blocks on its setup pipe before applying Seatbelt to a hidden target wrapper, which executes the built-in relay, a configured relay, or the standalone command.
The executable remains outside Seatbelt and waits for that direct child to exit.
The manager records the root and every descendant identity it observes by PID and process start time.
Once observed, a descendant remains a cleanup target after changing process group or session.
The manager also adopts the private temporary-directory guard for the lifetime.

The sandbox launcher is the sole host-side owner and retains the manager process and direct sandbox root as waitable children.
After the manager reports readiness, the launcher starts monitoring manager exit and relinquishes its duplicate temporary-directory guard.
The manager's adopted guard is then the only directory-cleanup owner; launcher-side fallback retains no directory-cleanup state.
After readiness, the launcher holds the control socket open only as the live-sandbox ownership token.
The launcher supports a hidden `--exit-with-parent <PID>` mode, which the server uses for one launcher subprocess per worker generation.
It verifies and captures its exact parent identity before creating the sandbox, watches that identity for exit, and revalidates it after watch registration and immediately before releasing the target.
The server closes unrelated inherited descriptors before launcher exec and then owns only the launcher's piped input and output, inherited error stream, and normal child exit, signaling, and reaping.
The runner starts with the original target stdin already attached.
After spawning it, the owned launcher replaces its own nonterminal input with `/dev/null`, while retaining output through manager cleanup.
After spawning the target, the runner drops its owned stdin and the launch command that held it.
No input copy in either host process masks relay input closure from the server.
Terminal stdin remains available to the launcher for foreground restoration.
The relay owns its direct worker and local transports.
All process-group and observed-descendant cleanup belongs to the sandbox lifetime, including descendants that retain worker streams after direct-worker exit.
The configured target may wrap the relay in another process; the relay need not be the sandbox root or process-group leader.

## Startup

The launcher starts the runner with `--bootstrap-fd <N>`, blocked on an inherited setup pipe, then launches the manager with the root PID, cleanup timeout, and private-directory path as native command arguments.
The manager derives the owner PID from its parent, while the private inherited Unix socket carries readiness and then remains open as the ownership token.
The manager validates the direct-child relationship and exact root identity, installs root and descendant tracking plus control-socket observation, adopts the directory guard, and reports readiness.
After receiving readiness, the launcher installs manager-failure recovery while the direct root remains live and waitable, relinquishes its duplicate guard, and revalidates the owner identity.
An owned `SIGTERM` already pending at this point requests retirement without sending setup.
Otherwise the launcher writes the complete protocol-2 frame: a four-byte unsigned big-endian length followed by 1 through 1,048,576 bytes of UTF-8 JSON.
The runner is spawned before writing because a valid frame can exceed pipe capacity.
The launcher makes one nonblocking write per event-loop iteration and checks owner exit, launcher signals, root exit, and manager recovery between writes, even when the reader keeps the pipe writable.
Level-triggered write readiness lets partial writes continue while capacity remains and blocks the event loop when the pipe is full.
An owned retirement request remains observable while a setup reader is stopped or unresponsive; the setup channel adds no writer thread or separate control message.
The runner consumes exactly that frame, closes its setup descriptor before native setup, and starts the target without waiting for EOF.
The launcher closes its writer after sending; there is no later release payload, descriptor transfer, acknowledgment, or persistent runner control channel.
On failure or cancellation before release, the launcher retains the setup writer through lifetime cleanup and closes it on return.
This stops the waiting root before pipe closure can race a truncated-frame diagnostic against the cancellation error.

The setup pipe is close-on-exec in the parent.
The runner command owns its read end until spawn returns; a child-only inheritance exception preserves that dynamically allocated descriptor and sanitizes unrelated descriptors.
Dropping the command promptly closes the launcher's unused read end.
The manager inherits neither pipe end, and the final target and relay never see the setup resource.
Original fd 0, 1, and 2 carry only target streams; stdin retains its open file description, offsets, seekability, and terminal identity.
Rust startup supplies `/dev/null` when the caller closed stdin before invoking the launcher.

The hidden `sandbox-target` wrapper only restores the original macOS signal mask from the validated unsigned `--signal-mask <MASK>` argument and execs the target command after `--`.
It neither reads setup nor replaces stdin, and wrapper arguments do not reach the requested command.
The runner retains the blocked forwarded signals while waiting; inherited dispositions still come from the launcher's child configuration.
Configured sandbox code therefore cannot run before manager observation is active and failure recovery is installed.

Signal delivery has a known startup limitation: before the runner spawns its target, it can be the only member of the target process group.
A group-directed `SIGHUP`, `SIGINT`, `SIGQUIT`, or `SIGTERM` received in that interval remains pending in the runner and is neither inherited nor replayed to the target.
This includes terminal-generated signals after foreground ownership has transferred, and signals relayed by the launcher; an early Ctrl-C can therefore leave the requested command running once startup completes.
Exactly-once signal delivery applies after the target exists.
Owned cancellation through launcher-addressed `SIGTERM` or owner exit uses lifetime retirement and remains available during setup writes.
The one-shot runner contract supplies no target-readiness acknowledgment or signal replay; extending that contract is outside the current integration.

The manager control socket carries no messages after readiness; it remains open only as an ownership token, and owner EOF requests retirement.
Abrupt owner loss before readiness closes the control socket and setup pipe before configured code runs, but private-directory cleanup is not guaranteed.
Before readiness, the launcher retains its guard and preserves it whenever manager adoption is ambiguous.
After readiness, the launcher relinquishes that guard and the manager becomes the sole directory-cleanup owner.
The adopted guard preserves on unexpected unwind and is armed for removal only after the manager proves cleanup.

Darwin cannot resolve every later fork atomically.
A descendant that becomes orphaned before the manager resolves its fork event remains outside the implemented guarantee.

The requested target runs in a dedicated process group.
Its root waiter blocks in `kevent()` for direct-root exit, signals addressed to the launcher, and the configured parent identity in owned mode.
The ordinary launcher consumes pending `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` and relays them to the target group.
After target creation, the private executable keeps these signals blocked while the target restores its original mask before exec, so each signal reaches the command once and the executable retains the waitable group identity through cleanup.
The startup limitation above applies before target creation.
In owned mode, parent exit or launcher-addressed `SIGTERM` requests managed retirement instead; the other supported signals retain their relay behavior.
When the launcher exclusively owns its foreground process group, it transfers controlling-terminal ownership to the target group; when a pipeline peer shares that group, it leaves terminal ownership unchanged.
The manager owns descendant cleanup and the private directory; the launcher preserves the direct command's status after natural completion and owns terminal state and signal relay.
Stopped/continued job state and general shell-pipeline job control remain unsupported.

## Retirement

Normal restart, automatic replacement, orderly shutdown, relay failure, and abrupt server exit all retire the same launcher-owned lifetime.
For a worker generation, the server first requests graceful shutdown through the relay protocol.
If the relay misses its deadline, the server sends `SIGTERM` to the launcher; owned mode interprets it as a managed-retirement request and remains alive through cleanup.
The manager closes the original root process group as a race backstop and retires every observed identity within the configured timeout.
During launcher-controlled retirement, the launcher retains the waitable root through cleanup and reaps it last.

The launcher closes its control endpoint to request retirement; abrupt launcher loss produces the same EOF.
The manager's single thread receives root, descendant, and control-readiness events from one `kqueue`.
Natural root exit first retires observed descendants and then closes the original process group; owner EOF with a live root closes the group and stops the root before draining observed descendants.
After clean natural-root cleanup, the manager waits for owner EOF before attempting directory removal and exiting.
Successful manager process exit is the primary cleanup barrier before the owner reaps the direct root.
In owned launcher mode, the launcher keeps its signals blocked until manager cleanup and direct-root reaping finish, so successful launcher exit is the server's cleanup barrier for parent loss, explicit retirement, and natural root exit.
A handled parent-loss or `SIGTERM` retirement request returns launcher status 0 after that barrier; natural root completion continues to return the root status.
If the launcher itself is killed or crashes, the manager still receives ownership-token EOF and performs cleanup, but the server can no longer wait synchronously for manager completion.

The manager preserves the private directory on unexpected unwind or any cleanup error because a surviving process may still use it.
It arms the adopted guard for removal only after successful cleanup proves that the directory is unused.
Directory removal is best effort.
Removal errors do not change the process exit status, so the directory can remain after successful process retirement.

## Manager failure

Each launcher retains a blocking monitor for the manager process.
If the manager exits unsuccessfully while the launcher still retains a live, waitable root, the monitor reconstructs the root's current process tree and performs bounded process cleanup before the launcher continues.
The fallback revalidates process identities immediately before signaling and closes the still-pinned process group as a race backstop.
The fallback has no directory-cleanup state.
If the manager exits before completing its own cleanup and removal attempt, the directory remains because a detached descendant observed only by the failed manager may still be live.
If manager exit times out, the owner requests forced exit and allows one more bounded recovery interval.
If the manager still does not exit, the owner disables fallback recovery before releasing the root's PID pin and returns an error without joining the live monitor thread.
If bounded fallback recovery has already started, the owner retains the pin until it finishes instead.
That monitor still reaps the manager if it exits later.

This fallback can recover only descendants still reachable from the root's current ancestry.
It cannot reconstruct a descendant that detached before the manager failed.
For a standalone command, successful fallback preserves the root's signal-derived exit status; a fallback error wakes the launcher with an error and leaves the directory in place.

## Standalone job control

An ordinary direct invocation gives the requested command its own process group.
When the launcher's foreground process group has no peer, it transfers foreground-terminal ownership before exec so terminal-generated signals reach the command group directly.
When a pipeline peer shares the launcher's foreground group, the launcher leaves terminal ownership unchanged.
In ordinary mode, `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` addressed to the launcher are blocked, consumed synchronously, and relayed once to that group.
After root exit, the ordinary launcher restores its own foreground group when it transferred ownership, drains forwarded signals already pending at that boundary, restores its inherited signal mask, and closes the ownership token to request manager cleanup.
If startup or recovery cleanup stops the root, the launcher drains pending forwarded signals before restoring the mask and returning the error.
A signal received after that final drain can then follow its inherited disposition; if that terminates the launcher, the manager completes lifetime cleanup.
With `--exit-with-parent`, `SIGTERM` is reserved for managed retirement rather than relayed.
The launcher closes the ownership token, waits for manager cleanup, reaps the direct root, and only then drains pending signals and restores its inherited mask.

## Policy extensions and compatibility

The pinned runner uses the base and preferences policies in `codex-rs/sandboxing/src/seatbelt*.sbpl` at `4a061c4ec94f5ad99e148982168ea4f21367c26c`.
MCP Console supplies read access to the filesystem root, restricted networking with no proxy, and its trusted `policy_extensions.sbpl`.
Managed networking and configurable user policies remain planned work.

Compared with the previous `read_only_policy.sbpl`, the native base already provides the CPU and R startup sysctls (including `hw.logicalcpu` and `kern.usrstack64`), Python's `kern.sysv.semmns`, POSIX semaphores, OpenMP shared memory, process permissions, `/dev/null`, PTY allocation, user lookup, power-management lookup, and read-only preferences.
Those local rules became redundant; their deletion does not mean the applications stopped needing them.
The unscoped semaphore permission still allows same-user semaphore interaction, as before.
The native base's broad host-PTY ioctl grant is overridden locally: host-terminal read and mutating ioctl stay denied, while slaves created inside the sandbox retain read, write, and ioctl through the PTY extension.
Attribute queries and the launcher's foreground-terminal handling remain usable.
The CLI host-terminal regressions and processx PTY workflow cover both sides of this boundary.

The temporary directory is disposable storage, not a writable workspace anchor.
MCP Console grants `file-write*` on its canonical private path through SBPL, rather than adding a native writable-workspace entry that protects the root and metadata directories.
The command can remove and replace that root and `.git`, `.agents`, and `.codex` directories inside it; the manager still attempts cleanup at lifetime end.
The CLI temporary-directory replacement and host-hard-link tests cover these semantics.

Each remaining exception has a comment beside its rule.
Evidence has different strengths:

| Exception                               | Workflow and evidence                                                                                                                           | Remaining uncertainty                                                     |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `kern.boottime`                         | processx loads ps; `r-lib/ps/src/api-macos.c::ps__boot_time` reads `KERN_BOOTTIME` and throws on failure. Covered by the processx PTY workflow. | None about the read; package versions may change the loading path.        |
| `machdep.ptrauth_enabled`               | Pointer-authentication probe retained from the original Apple Silicon runtime observations.                                                     | The individual native library was not recorded.                           |
| `kern.ngroups`                          | Group-limit query attributed by the previous policy to Quarto's platform-probe workflow.                                                        | The exact utility and current necessity remain unconfirmed.               |
| `sysctl.oidfmt.*`                       | `sysctl(8)` queries OID type/format metadata, including for binary output used by R/Quarto probes.                                              | A particular rendering run may not use this path.                         |
| `sysctl.name.*`                         | `sysctl(8)` translates numeric OIDs into printable names; separate from format metadata.                                                        | A particular rendering run may not use this path.                         |
| `security.mac.lockdown_mode_state`      | Lockdown Mode query observed during the original uv system-configuration startup.                                                               | Exact framework call site and current fatal dependency unconfirmed.       |
| `kern.bootargs`                         | Boot-argument query observed during that startup workflow.                                                                                      | Exact framework call site and current fatal dependency unconfirmed.       |
| `/dev/dtracehelper` write               | Device write access observed during uv SystemConfiguration startup.                                                                             | Exact operation and current fatal dependency unconfirmed.                 |
| `/dev/dtracehelper` ioctl               | Previous policy records EPERM in the Quarto wrapper/child-utility workflow.                                                                     | The utility and ioctl number were not recorded.                           |
| `com.apple.SystemConfiguration.configd` | uv's HTTP client discovers macOS proxy settings through SystemConfiguration. The uv lockfile retains the `system-configuration` dependency.     | The offline install test does not prove every framework lookup is needed. |
| `com.apple.logd`                        | Logging endpoint observed during the original uv startup workflow.                                                                              | Exact caller and current fatal dependency unconfirmed.                    |
| `com.apple.system.notification_center`  | Notification endpoint observed separately during that workflow.                                                                                 | Exact registration and current fatal dependency unconfirmed.              |

The original observations are preserved in commit `6e8ece1a8deec6064ad0c881c97889ed0745b142`'s policy comments.
They establish why the exceptions were introduced, not that every current runtime requires each one.
The processx, R, Python, generated Quarto document, and sandboxed uv offline-wheel-install tests exercise actual workflows; host resolver tests alone cannot validate sandbox compatibility.
None of the remaining named exceptions is supplied by the pinned base.
To remove one as unnecessary, reproduce its motivating workflow without that permission and check the platform/runtime versions involved; to remove it as redundant, verify that the new pinned base supplies it.
Unresolved attribution is retained explicitly rather than assigning a probe to an unsupported package guess.

## Scope

One launcher-owned implementation serves sandboxed built-in and custom worker relay generations and direct `mcp-console sandbox` invocations.
The server invokes the launcher with the relay command line as its target and has no in-process sandbox construction, manager handle, setup pipe, root identity, temporary-directory guard, or manager-recovery state.
The launcher retains the manager-owned process-group race backstop, with launcher fallback after manager failure, and gates the target before any configured code runs.
The direct path retains inherited standard streams, uses a dedicated target process group, and supplies the foreground-terminal and signal behavior above.
Hidden owned mode adds exact parent-exit observation and a `SIGTERM` retirement request without another control descriptor.
The relay receives only its standard streams from the launcher.
The runner's initial JSON configuration requires UTF-8 command arguments, paths, and environment values; the launcher rejects unsupported values before spawning it.
Standard-stream contents remain arbitrary bytes.
Any future sandbox-specific control channel must terminate at the sandbox process boundary; its transport and setup mechanism are independent of the relay protocol.
The launcher does not support `Ctrl-Z` followed by `fg` or general pipeline job-control semantics.
Linux and Windows are not supported.
