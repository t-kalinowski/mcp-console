# Linux host compatibility

This review starts at MCP Console main `f35a304d` and its actual runner pin `d488fc969da435f93ea5937c7f284fa91a8c2575`.
The coordinated [runner changes](https://github.com/t-kalinowski/codex/compare/d488fc969da435f93ea5937c7f284fa91a8c2575...7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc) advance the pin to `7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc`.
Default execution still uses bubblewrap and the requested filesystem/network policy.
Namespace or policy failure never selects an unrestricted target or a different backend.

| Host condition                                            | Previous integration                                      | Current behavior                                                                              |
| --------------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| Fresh namespace-local procfs                              | Supported                                                 | Supported                                                                                     |
| Native procfs preflight selects inherited procfs          | Rejected at the target hook                               | Supported with the same namespace and policy enforcement                                      |
| `pidfd_open` unavailable or denied                        | Startup fails                                             | Native control and direct-child waits remain available                                        |
| `pidfd_send_signal` unavailable or denied                 | Retirement fails                                          | Native channel requests retirement; completion still requires the child wait                  |
| Host subreaper unavailable                                | Startup fails                                             | No host subreaper requested                                                                   |
| Host child lists or namespace PID discovery unavailable   | Cleanup depended on host child lists                      | No child-list or `NSpid` discovery                                                            |
| No native child completion evidence                       | Could mistake a missing child-list file for an empty list | Nonzero failure and retained private storage                                                  |
| Stopped namespace init, pidfds unavailable                | Startup already unsupported                               | Retirement deadline fails explicitly; private storage remains                                 |
| Explicit Landlock filesystem/network backend              | Rejected                                                  | Direct exec, native policy checks, no process isolation or supervised lifecycle               |
| Unavailable namespace operations with bubblewrap selected | Failure                                                   | Failure; no backend switch                                                                    |
| Suitable host helper with a damaged unused bundle         | Launch rejected                                           | Native host-helper selection succeeds; selected bundled helpers still require an exact digest |

The tested Linux baseline is x86_64 Ubuntu with kernel `6.8.0-139-generic`.
This is a tested baseline, not a claimed minimum.
Namespace permissions, procfs availability, seccomp, and the selected backend's enforcement capabilities determine whether a host works.
The constrained-host tests deny pidfd and subreaper syscalls in the actual runner and helpers.
Fault fixtures also withhold native wait status and stop namespace init.
A kernel version string cannot establish these permissions.

Before removing the procfs restriction, differential probes exercised the pinned native helper with fresh procfs and its supported inherited-procfs mode.
They used disposable same-user processes, an explicitly ptraceable synthetic host fixture, synthetic environment/memory/file data, an open writable host file, a control pipe, and a loopback listener.
The matrix covered root-readable and denied-sentinel filesystem policies, each with restricted and enabled networking.

Inherited procfs exposed host PIDs.
Access through host `environ`, `root`, `cwd`, file descriptors, and memory remained denied.
File writes, host control-pipe writes, signals, ptrace, process-memory syscalls, and network-namespace entry did not bypass the selected policy.
Direct file reads and loopback connections followed the requested read and network permissions.
Readable process metadata was evaluated against the declared read policy.

The public Console executable tests repeat the matrix against the supervised runner, including a real nested mount that makes the native procfs preflight choose its supported fallback.
They also attempt to read the supervisor's launch environment, write its memory, acquire an intentionally inherited host control pipe, and signal it.
Listing fd-directory names can succeed under the root-read policy; following those links and accessing the control endpoint remain denied.
Changing the transport-looking environment variable inside the target does not change accepted policy.

Explicit Landlock is a different user-selected backend with native direct-exec semantics.
The tested kernel allowed same-user host signalling in that mode.
It must not substitute for bubblewrap when process isolation or descendant retirement is required.
The runner rejects private storage, caller-death observation, retirement SIGTERM, an explicit cleanup deadline, and managed proxy routing with Landlock.
Native rejection of restricted-read policies is preserved.
Restricted filesystem policies also require the native truncate capability (Landlock ABI 3 or later); older best-effort enforcement would leave file truncation unrestricted.
The tested host provides ABI 4.
Native device-ioctl restrictions depend on ABI 5 and are not part of this backend's portable contract.

The downstream package builds and strips its bundled helper first, embeds that exact helper digest in the runner, and verifies the selected bundled executable through the same open descriptor used for exec.
A suitable trusted host helper retains native precedence; native discovery still excludes helpers beneath the working directory.
An unused missing or modified bundled helper does not prevent host-helper execution; a selected modified helper fails verification without fallback.


## Reproducing the comparisons

`tests/fixtures/cli/sandbox/procfs.py` owns the disposable host process, sentinel data, listener, and attack attempts.
`tests/support/linux_sandbox.py` owns fresh/nested namespace capability probes and the inherited-procfs fixture.
The public cases share policy assertions and record complete operation outcomes:

```console
scripts/test cli/sandbox/test_procfs
scripts/test cli/sandbox/test_configuration
python3 tests/sandbox_installation.py target/release/mcp-console
```

The procfs matrix has four policy combinations per procfs view: host-readable or sentinel-denied filesystem, each with restricted or enabled networking.
The host fixture allows ptrace explicitly so host Yama restrictions cannot substitute for sandbox enforcement.
The controller verifies unchanged synthetic files and memory, no host signals, and untouched host control pipes.
The inherited fixture mounts an outer procfs and masks `/proc/sys`, preventing a fresh inner proc mount while allowing the native inherited-procfs path.

The security comparison first ran against the pinned native backend before the integration restriction was removed: all eight combinations preserved the selected policy.
The updated supervised executable repeats both views and checks supervisor configuration and control access.
These probes establish the tested interfaces and policy matrix, not a general proof against every kernel attack.

Runner contracts additionally exercise actual seccomp-denied pidfd/subreaper syscalls, namespace denial without a backend switch, native wait failure, stopped namespace init without pidfds, explicit Landlock policy rejection, and unavailable Landlock.
Shared lifecycle assertions remain shared; native interposition and namespace setup stay in Linux fixtures.
Transcript cases that require procfs process discovery or pidfd events declare those fixture capabilities separately from production requirements.

The Ubuntu host's AppArmor policy requires an existing `bwrap` profile for namespace creation.
Host runs use a test-only launcher for the exact built helper under that profile and the previously documented `R_PROFILE_USER=/dev/null` runtime setting.
Nested procfs and bundled-helper installation checks run in a disposable container permitting namespace operations; no shipped helper or host security policy is changed.
Host restrictions that deny native namespaces remain explicit launch failures.

## Validation record

Validation used the pinned sources above on macOS 26.6.2 arm64 and Ubuntu x86_64 with kernel `6.8.0-139-generic` and Landlock ABI 4.

| Check                                       | Result                                                                                                              |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Runner and native Linux scoped suites       | 223 passed; scoped Clippy passed                                                                                    |
| Runner macOS suite                          | 68 passed; scoped Clippy passed                                                                                     |
| Console macOS aggregate `scripts/check`     | Passed, including source and wheel installations                                                                    |
| Console Linux core checks                   | Passed, including 47 Rust tests and the Python development gates                                                    |
| Console Linux public transcripts            | All capability-applicable cases passed; all 439 retained snapshots matched the previous behavior after regeneration |
| Fresh/inherited procfs policy matrix        | All eight combinations passed against the original native backend and the updated supervised Console executable     |
| Linux relocated bundle and helper integrity | Eight passed; one macOS-only case skipped                                                                           |
| Linux source and wheel installations        | Both installation cases passed in the namespace-capable disposable container                                        |

The coordinated [runner CI run](https://github.com/t-kalinowski/codex/actions/runs/34431093727) passed on macOS, including Bazel and release-artifact contracts.
Its Linux job passed all 77 runner contracts and 207 native tests, then failed before Bazel test compilation because the pinned external Ubuntu `zlib1g_1.3.dfsg-3.1ubuntu2.1_amd64.deb` download returned HTTP 404.
Later Linux release-artifact and lint steps in that job were skipped; local scoped Clippy and downstream Linux source/wheel checks passed separately.
