# Sandbox integration

MCP Console selects its application policy, verifies the installed private executable, and replaces `mcp-console sandbox` with that executable on macOS and Linux.
The runner owns native enforcement, signal and terminal handling, descendant observation and retirement, and private storage.
Console contains no sandbox manager, recovery monitor, descendant tracker, target signal wrapper, or directory owner.
The [integration validation record](SANDBOX_RUNNER_INTEGRATION.md) records the baseline, supported-host results, fixture changes, and changed guarantees.

## Application policy and launch

The pin in `sandbox-runner.json` selects the runner source commit, protocol 2, and Rust 1.95.0.
The executable contract and acceptance tests at that commit are the source of truth for runner behavior.
Without a public configuration option, Console uses `--config-env MCP_CONSOLE_SANDBOX_CONFIG -- COMMAND [ARG]...`.
The selected variable contains one immutable JSON object, consumed by the runner and removed from the target environment.
It is reserved for this private handoff.
Arguments, cwd, and the rest of the environment remain ordinary executable inputs.
There is no configuration file, mutable path reference, bootstrap writer process, or application control channel.
Linux namespace init retains a private runner control endpoint that never reaches target code.

The public `sandbox --config-env NAME -- COMMAND [ARG]...` option selects an explicit complete configuration using the runner's canonical schema.
See [sandbox configuration](SANDBOX_CONFIGURATION.md) for fields, defaults, trust boundaries, size limits, and runnable shell, Python, and R examples.
`serve` supplies the default configuration plus any explicit writable roots; ambient values never select its policy.

By default, Console requests:

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
The temporary [`--writable-root DIR`](SANDBOX_CONFIGURATION.md#additional-writable-directories) option augments the filesystem entries with explicit write access on `serve` and `sandbox`.
The server retains the resolved path list across worker generations and only forwards it to the sandbox frontend; the relay and worker do not interpret it.
These directories are persistent user data and are never removed by sandbox retirement.

The frontend preserves its PID and direct caller across exec.
The server still launches one ordinary child with piped stdin/stdout and inherited stderr for each worker generation.
It requests graceful relay shutdown, then runner retirement through SIGTERM if required, and observes and reaps that child.
Cancellation before worker readiness also requests runner retirement and waits for its exit before joining worker I/O.
The runner inherits the original fd 0, 1, and 2; Console does not copy, frame, relay, or retain those streams.
The runner restores target signal state and handles native terminal ownership.
Version 2 requires UTF-8 executable arguments, paths, and environment values.
The selected JSON is limited to 1 MiB and shares the native exec byte budget with arguments and the launch environment.
The runner's separate private framed-descriptor interface still accepts requests up to 1 MiB independently of stdin.
It remains available for larger integrations; Console uses environment transport for its small default policy.

Console verifies installation-relative runner lookup, source provenance, target, runner digest, and license artifacts.
The runner retains native trusted-PATH helper selection and verifies a selected bundled helper through the same open descriptor it executes.
Its expected digest is embedded during staging; an unused bundled helper does not block a suitable host helper.
Missing or mismatched selected artifacts fail before target execution.
See [release preparation](../RELEASE.md#private-sandbox-executable).

## Supported hosts and lifetime limits

macOS uses Seatbelt and a native stage that execs the requested target in the same PID.
The target leads its dedicated process group; the runner remains outside that group.
The runner transfers an exclusively owned foreground terminal to the target group and restores it at retirement.
With a foreground peer, it keeps the caller group in control and forwards terminal signals.
General shell job suspension and resumption are unsupported.

Linux uses the native namespace helper, bubblewrap, and native namespace retirement.
The runner waits for its known direct native child; it does not require a host subreaper, process-tree enumeration, or namespace-PID discovery.
Fresh procfs is optional: the supported inherited-procfs path retains user, mount, and PID isolation, though host PIDs and permitted metadata may be visible.
Pidfds provide optional emergency termination of a stopped namespace init.
Without that capability, a stopped init can exceed the retirement deadline; the runner reports failure and retains private storage.
Missing native wait evidence never establishes cleanup.
The relay-worker sideband uses two anonymous pipes under the same sandbox policy.
It does not require Unix socket syscall exceptions or relaxed network restrictions.
The selected helper's namespace operations, procfs, and requested seccomp/network capabilities must be available.
See [Linux compatibility](LINUX_COMPATIBILITY.md) for differential security results and tested baselines.
Full-disk-write policies remain rejected by supervised execution because writable procfs could expose supervisor resources.
Linux keeps the caller's foreground-terminal ownership and relays interrupts through namespace init.
Standalone callers may explicitly select `linux_backend: "landlock"` for native filesystem/network enforcement with direct-exec semantics.
This mode has no process isolation or descendant cleanup and rejects supervised-lifetime and proxy options.
The default server never selects it.
See [configuration](SANDBOX_CONFIGURATION.md).
Windows and other operating systems remain unsupported.

Configured caller death must retire the workload while the runner lives.
When neither stdin nor stdout is a terminal, a runner with a configured caller enters its own process group before native setup.
This includes the server's piped launch with inherited terminal stderr.
The runner preserves its PID, parent, session, and stream descriptions, and survives signals addressed to the caller's group so it can retire the workload on caller death.
To interrupt that workload without killing the caller, direct the signal to the runner.
Unowned launches and launches with terminal stdin or stdout retain their previous group behavior.

Caller death is distinct from loss of the supervisor itself.
The sole runner has no independent recovery process.
On Linux, native parent-death links terminate the workload after native readiness if the runner receives SIGKILL.
Earlier bubblewrap startup windows remain outside that termination guarantee; the release gate still prevents an unreleased target from executing after runner death.
On macOS, supervisor death does not guarantee descendant termination.
Neither platform guarantees directory removal or terminal restoration after runner death, or recovery from a stopped or hung runner.
Surviving processes retain native sandbox enforcement.
The server's final forced child termination cannot establish a successful cleanup barrier.

The runner reports discovered cleanup failures on stderr with a nonzero status, including directory-removal failure.
It retains private storage when retirement cannot be established.
The previous Console implementation treated directory removal as best effort.
Darwin still cannot guarantee discovery of a descendant that detaches and becomes orphaned before observation, or atomic identity-check-and-signal delivery.
The runner keeps the native root unreaped through retirement, preserving the owned process group, and retires detached descendants it has already observed.
Startup failures use the runner's native diagnostics; successful cancellation can be silent.

## Policy extensions and compatibility

The pinned runner uses the base and preferences policies in `codex-rs/sandboxing/src/seatbelt*.sbpl` at `7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc`.
MCP Console supplies read access to the filesystem root, restricted networking with no proxy, and its trusted `policy_extensions.sbpl`.
The default application policy remains fixed.
Standalone callers can select an explicit configuration through `--config-env`; managed proxy support follows the runner schema.

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
