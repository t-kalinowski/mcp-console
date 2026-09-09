# Sandbox integration

MCP Console selects its application policy, verifies the installed private executable, and replaces `mcp-console sandbox` with that executable on macOS and Linux.
The runner owns native enforcement, signal and terminal handling, descendant observation and retirement, and private storage.
Console contains no sandbox manager, recovery monitor, descendant tracker, target signal wrapper, or directory owner.
The [integration validation record](SANDBOX_RUNNER_INTEGRATION.md) identifies retained contracts that the current pin does not yet pass; this migration is not ready to merge.

## Application policy and launch

The pin in `sandbox-runner.json` is `ee88fa4f7744fdfb0e99dd0e928ff43d7171814e`, protocol 2, Rust 1.95.0.
The executable contract and acceptance tests at that commit are the source of truth for runner behavior.
Console uses `--config-env MCP_CONSOLE_SANDBOX_CONFIG -- COMMAND [ARG]...`.
The selected variable contains one immutable JSON object, consumed by the runner and removed from the target environment.
It is reserved for this private handoff.
Arguments, cwd, and the rest of the environment remain ordinary executable inputs.
There is no configuration file, mutable path reference, bootstrap writer process, or persistent control channel.

Console requests:

- restricted filesystem access with host reads;
- restricted networking, without a managed proxy;
- the existing trusted macOS policy extension;
- runner-owned private storage exported as `TMPDIR`;
- a 1000 ms descendant-retirement timeout;
- optional exact caller observation from `--exit-with-parent PID`; and
- SIGTERM retirement in that owned mode, or ordinary signal forwarding otherwise.

`MCP_CONSOLE_SANDBOX=1` remains part of the target environment.
The frontend continues to remove `DYLD_INSERT_LIBRARIES` and `LD_PRELOAD` before runner exec.
The runner supplies full mutation of its private storage; Console does not construct or remove its path.
The temporary layout is a runner-owned `sandbox-XXXXXX` container with a writable `data` child.

The frontend preserves its PID and direct caller across exec.
The server still launches one ordinary child with piped stdin/stdout and inherited stderr for each worker generation.
It requests graceful relay shutdown, then runner retirement through SIGTERM if required, and observes and reaps that child.
The runner inherits the original fd 0, 1, and 2; Console does not copy, frame, relay, or retain those streams.
The runner restores target signal state and handles native terminal ownership.
Version 2 requires UTF-8 executable arguments, paths, and environment values.
Console adds no request-size cap; the configuration and ordinary launch inputs remain subject to native exec limits.
The runner's separate framed-descriptor interface still accepts requests up to 1 MiB and is exercised by installation tests, but Console does not need it for its small fixed policy.

Installation-relative lookup, source provenance, target validation, executable digests, and bundled Linux helper verification remain in Console.
A missing or mismatched artifact fails before launch.
See [release preparation](../RELEASE.md#private-sandbox-executable).

## Supported hosts and lifetime limits

macOS uses Seatbelt and a native stage that execs the requested target in the same PID.
The target leads its dedicated process group; the runner remains outside that group.
The runner transfers an exclusively owned foreground terminal to the target group and restores it at retirement.
With a foreground peer, it keeps the caller group in control and forwards terminal signals.
General shell job suspension and resumption are unsupported.

Linux uses the native namespace helper, bubblewrap, namespace-local procfs, a host subreaper, and pidfds.
It requires kernel 5.11 or later and permission for user, mount, PID, and network namespaces.
Full-disk-write policies and procfs fallback are rejected by the runner's supervised path; Console's fixed policy requests neither.
Linux keeps the caller's foreground-terminal ownership and relays interrupts through namespace init.
Windows and other operating systems remain unsupported.

Configured caller death must retire the workload while the runner lives.
This is distinct from loss of the supervisor itself.
The sole runner has no independent recovery process: SIGKILL, a crash, or an unresponsive runner does not guarantee descendant cleanup, directory removal, or terminal restoration.
Surviving processes retain native sandbox enforcement.
The server's final forced child termination cannot establish a successful cleanup barrier.

The runner reports discovered cleanup failures on stderr with a nonzero status, including directory-removal failure.
It retains private storage when retirement cannot be established.
The previous Console implementation treated directory removal as best effort.
Darwin still cannot guarantee discovery of a descendant that detaches and becomes orphaned before observation, or atomic identity-check-and-signal delivery.
The observed cleanup regressions in the validation record remain blockers; they are not accepted changes to the retained Console contract.

## Policy extensions and compatibility

The pinned runner uses the base and preferences policies in `codex-rs/sandboxing/src/seatbelt*.sbpl` at `ee88fa4f7744fdfb0e99dd0e928ff43d7171814e`.
MCP Console supplies read access to the filesystem root, restricted networking with no proxy, and its trusted `policy_extensions.sbpl`.
Managed networking and configurable user policies remain planned work.

Compared with the previous `read_only_policy.sbpl`, the native base already provides the CPU and R startup sysctls (including `hw.logicalcpu` and `kern.usrstack64`), Python's `kern.sysv.semmns`, POSIX semaphores, OpenMP shared memory, process permissions, `/dev/null`, PTY allocation, user lookup, power-management lookup, and read-only preferences.
Those local rules became redundant; their deletion does not mean the applications stopped needing them.
The unscoped semaphore permission still allows same-user semaphore interaction, as before.
The native base's broad host-PTY ioctl grant is overridden locally: host-terminal read and mutating ioctl stay denied, while slaves created inside the sandbox retain read, write, and ioctl through the PTY extension.
Attribute queries and the launcher's foreground-terminal handling remain usable.
The CLI host-terminal regressions and processx PTY workflow cover both sides of this boundary.

The temporary directory is disposable storage, not a writable workspace anchor.
The runner grants full mutation of its private `data` directory, including replacement of that directory and `.git`, `.agents`, and `.codex` inside it.
Console retains its host-terminal, sysctl, device, and service exceptions unchanged in `src/sandbox/policy_extensions.sbpl`.
Moving enforcement does not establish that any exception is unnecessary.
The CLI temporary-directory replacement and host-hard-link tests remain the compatibility constraints.

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
