# Linux host compatibility

The current policy contract below follows runner pin `2d0ad797210de821c07d1f18e4f1ffdcf06589cb` from [`sandbox-runner.json`](../sandbox-runner.json).
This advances `3f060c4210deba4d55cb6ec6d19899721182afae` while retaining protocol 2 and the `rust-v0.154.0` release base.
The [historical validation record](#validation-record) identifies the earlier Console and runner revisions used for the host comparison.

## Policy and backend contract

Use `mcp-console sandbox --config-env NAME -- COMMAND ARGS...` to supply a complete policy.
For managed policies, omitting `linux_backend` and explicitly selecting `"bubblewrap"` both use the supervised namespace path, including with unrestricted filesystem and network access.
External enforcement without a proxy uses ordinary process supervision and delegates isolation to the outer sandbox, even with explicit `"bubblewrap"`.
Selecting `"landlock"` uses direct native execution, with no bubblewrap process isolation or supervised descendant retirement.
Neither backend switches automatically or retries the target without enforcement.
The native inherited-procfs alternative retains bubblewrap and the selected policy.

| Source of behavior                | Contract                                                                                                                                                                                            |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Console defaults                  | Host reads, restricted networking, private writable `TMPDIR`, and bubblewrap supervision. Home and the working directory receive no write grant. An explicit configuration replaces these defaults. |
| Native bubblewrap/seccomp backend | Filesystem mounts and masks, namespace isolation, and requested network restrictions. Writable roots retain native metadata protections and mount prerequisites.                                    |
| Runner managed execution          | Supervised launch and namespace retirement, optional caller-death observation and private storage. Full filesystem access retains native setup/control and namespace init.                          |
| Runner external execution         | Without a proxy, no native enforcement: retire the original process group and wait for the direct child. The outer sandbox owns escaped descendants and both filesystem and network enforcement.    |
| Native Landlock/seccomp backend   | Direct filesystem/network enforcement, with native rejection of restricted-read and other policies that its legacy representation cannot preserve. No namespace process isolation.                  |
| Runner's Landlock integration     | Rejects external enforcement, supervised lifecycle options, and managed proxy routing. Policies requiring filesystem restrictions must have truncate enforcement (Landlock ABI 3 or later).         |

Networking and filesystem access are independent: `network: "enabled"` does not grant writes.
The [configuration reference](SANDBOX_CONFIGURATION.md#explicit-linux-backend-selection) distinguishes omitted lifecycle defaults from explicit requests.

### Filesystem classification

The runner converts the raw filesystem policy and network setting into the canonical permission profile before selecting execution.
Classification depends on effective permissions, not the `kind` string alone.
Here, **root** means the special entry `{"type":"special","value":{"kind":"root"}}`.

| Filesystem policy                                                               | Linux execution                                                                                                                                                     |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kind: "unrestricted"`                                                          | Full filesystem access with native supervision and the selected network policy. Entries accompanying this kind do not narrow access.                                |
| `kind: "external-sandbox"`                                                      | Delegated filesystem and network enforcement without a proxy; native managed-network enforcement when a proxy is supplied. Entries do not narrow filesystem access. |
| `kind: "restricted"`, root `write`, no effective narrower rule                  | Classified as full filesystem access and supported with native supervision.                                                                                         |
| Root `write` plus a path `read` overridden by `write` at the same target        | Still full write access. A root `read` alongside root `write` likewise does not narrow writes.                                                                      |
| `kind: "restricted"`, root `write` with effective read-only or denied carveouts | Remains restricted and proceeds to native policy/mount validation. Supported carveouts retain their read/write restrictions.                                        |
| Root `read`, optionally with explicit writable directories                      | Supported subject to the native writable-root and host requirements.                                                                                                |

Native precedence matters: a same-target `write` overrides `read`, while `deny` takes precedence over both.
The full-write predicate recognizes the special root grant; a literal path entry for `/` is not that special entry and proceeds through writable-root preparation.
Classification alone does not establish that native setup can execute a policy.
Restricted root-write policies with carveouts still prepare protected metadata locations (`/.git`, `/.agents`, `/.codex`); absent mount targets can fail to be created on the host.
The public carveout regression supplies those targets as read-only mounts in an outer test namespace, without modifying the host root.

The native full-filesystem builder binds `/` writable and retains namespace init whenever target setup requires process isolation, including with enabled networking.
This supplies the setup/control exchange and retirement barrier previously missing from the full-write path.
Fresh procfs is not required for unrestricted execution; inherited procfs can expose host metadata and paths.
Full filesystem access can also let the workload alter shared files or influence unsandboxed processes, undermining network or supervisor restrictions.
The restricted-policy security results below do not establish those guarantees for unrestricted or externally enforced policies.
See [enforcement modes](SANDBOX_CONFIGURATION.md#filesystem-and-enforcement-modes) for proxy selection and external lifecycle limits.
The source boundaries are the pinned [runner execution selection](https://github.com/t-kalinowski/codex/blob/2d0ad797210de821c07d1f18e4f1ffdcf06589cb/codex-rs/mcp-console-sandbox/src/codex.rs), [policy classification and precedence](https://github.com/t-kalinowski/codex/blob/2d0ad797210de821c07d1f18e4f1ffdcf06589cb/codex-rs/protocol/src/permissions.rs), and [native mount builder](https://github.com/t-kalinowski/codex/blob/2d0ad797210de821c07d1f18e4f1ffdcf06589cb/codex-rs/linux-sandbox/src/bwrap.rs).

### Native built-in profiles

`extends: ":workspace"` and `extends: ":read-only"` use native constructors and a fixed, materialized workspace.
Console's workspace baseline adds a `.claude` read entry and explicitly excludes the shared `/tmp` and inherited `TMPDIR` write grants; runner-owned private temporary storage remains writable.
Native metadata exclusions survive broader enclosing writable roots, and explicit equal-path or descendant write grants retain native precedence.
Git pointer and symlink handling remain in the native implementation.

The current Linux mount backend does not fully implement read grants beneath broader read denials: an ancestor mask can hide the narrower readable directory, and adding a deeper denial can fail while creating its mount target.
The runner's public tests reproduce both outcomes with a selector and with equivalent complete raw policies.
macOS honors the narrower read grant in those cases.
Console forwards the native result without rewriting policy, creating placeholder directories, or changing the backend.
The native backend may create temporary mount placeholders for missing protected paths and removes them at retirement.

At the current pin, all 96 runner acceptance tests passed in a namespace-capable x86-64 Ubuntu 24.04 container on kernel `6.8.0-139-generic`; all 87 macOS runner tests also passed.
These counts include the profile and raw-policy comparisons.
The host's ordinary namespace restrictions required the test container; this does not establish support for restricted container or AppArmor configurations.

## Docker targets

An ordinary Docker container can deny the namespace operations needed by the default native backend.
Docker placement adds no privileged flags or implicit backend change.
Explicit `external-sandbox` without a proxy delegates filesystem and network enforcement to the owned container and works without nested namespace creation; `network: restricted` does not add a block in that delegated mode.
The adapter still runs native validation and reports an explicit proxy or native setup failure.
See [Docker execution](DOCKER.md) for configuration and current acceptance coverage.

## Earlier host compatibility comparison

The following comparison and validation record started at Console main `f35a304d` and runner `d488fc969da435f93ea5937c7f284fa91a8c2575`.
The coordinated [runner changes](https://github.com/t-kalinowski/codex/compare/d488fc969da435f93ea5937c7f284fa91a8c2575...7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc) advanced that pin to `7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc`.
These are historical host results, not validation of every later revision.

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

This historical validation used Console commit `61cd8fdfe4c32d16a8439a10419b10d02cd1e037` with runner pin `7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc` on the `rust-v0.150.1` release base.
The native comparison baseline was runner `d488fc969da435f93ea5937c7f284fa91a8c2575` from Console main `f35a304d`.
Validation ran on macOS 26.6.2 arm64 and Ubuntu x86_64 with kernel `6.8.0-139-generic` and Landlock ABI 4.

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
