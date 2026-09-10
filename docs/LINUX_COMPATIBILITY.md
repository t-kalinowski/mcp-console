# Linux host compatibility

## Artifact portability

The portability work starts at Console main `f21abe426014ba70ef2a1ab7419955396d385aab` and advances the [runner pin](https://github.com/t-kalinowski/codex/compare/7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc...a5bca6098099c40a1cad146c6e1d5e895bcac6ee) to `a5bca6098099c40a1cad146c6e1d5e895bcac6ee`.
It reuses that release's Linux matrix, `x86_64-unknown-linux-musl` and `aarch64-unknown-linux-musl`, and its existing `install-musl-build-tools.sh` recipe.
The recipe supplies Zig 0.14 for native dependencies, a musl GCC linker, and static libcap 2.75 with target-specific pkg-config paths.
The standalone runner now enables the same vendored OpenSSL dependency as the upstream release executable.
Its ancillary-message length conversions also accommodate the musl libc field types on both architectures.
No additional operating-system implementation is introduced.

Console release wheels deliberately pair a GNU application with static musl companions of the same architecture.
Staging records actual ELF linkage and rejects dynamically linked musl companions.
The build verifies target compatibility and all staged digests; the release audit checks the packaged executables and wheel tags again.
Every bundled file must also pass Console's existing launch-time integrity check, including bubblewrap when a system helper is available.
Native selection still prefers a compatible system helper and otherwise uses the bundled one.
It does not retry a failed sandboxed command with a different helper.

The following results were measured on 2026-09-10 using native x86-64 and AArch64 execution in privileged disposable Docker containers.
The x86-64 host used kernel `6.8.0-139-generic`; the AArch64 VM used `6.8.0-117-generic`.
These containers supplied namespace permissions; the results do not establish that an arbitrary host's security policy permits sandboxing.

| Artifact                              | ELF dependencies and version requirements                                            | Execution result on both architectures                                                                                                           |
| ------------------------------------- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Static musl runner and bubblewrap     | No interpreter, `DT_NEEDED`, or symbol-version requirements                          | Ran on Ubuntu 22.04/glibc 2.35, Ubuntu 24.04/glibc 2.39, and Alpine 3.22/musl 1.2.5; empty-PATH sandbox operation and binary stdin/stdout passed |
| GNU Console wheel                     | Native GNU loader, `libc.so.6`, `libgcc_s.so.1`; highest required GLIBC version 2.39 | Installed `manylinux_2_39_{x86_64,aarch64}` wheels passed bundle, relocation, integrity, and launcher checks on Ubuntu 24.04                     |
| GNU Console on Ubuntu 22.04           | Loader reports `GLIBC_2.39` not found                                                | Unsupported, even though its companions run there                                                                                                |
| Default static musl Console prototype | Static executable cannot load shared R                                               | A real R cell failed with `Dynamic loading not supported` on both Alpine architectures                                                           |
| Dynamic musl Console prototype        | Native musl loader and libc plus `libgcc_s.so.1`                                     | Native bundle and bare runtime probes passed below; repaired musllinux wheel layout and default managed runtime remain unqualified               |

The musl ARM prototype's versioned `GLIBC_2.0` reference is supplied by `libgcc_s.so.1`; its loader and libc dependency are musl.
Version names alone do not identify the required libc.
Release audits inspect the loader, dependencies, and symbol versions together, and do not infer compatibility from Rust target names.

## Complete runtime results

Manual probes exercised the installed executable through public MCP calls and default sandboxed `serve`.
They loaded R and Python, imported native NumPy and SSL extensions, shared state through the R/Python bridge, executed DuckDB SQL, prepared R `digest`, Python `packaging==25.0`, and DuckDB `fts`, interrupted active R and Python cells, and restarted the worker.
Restart checks covered state loss and retained requirements.
These were exploratory runtime checks; the release workflow retains its existing runtime smoke contract.

| Application and host combination                                                            | Result                                                                                                                                                                                                             |
| ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| GNU x86-64 wheel, Ubuntu 24.04, R 4.6.1, managed Python 3.12.13                             | Full installed R/Python/DuckDB, preparation, both interruptions, and restart passed                                                                                                                                |
| GNU AArch64 wheel, Ubuntu 24.04, R 4.6.1, managed Python 3.12.13                            | R/Python/DuckDB cells and all preparation passed; R interruption entered R's crash handler, so the complete combination is not qualified                                                                           |
| Dynamic musl native bundle, Alpine 3.22, R 4.5.0, system Python 3.12.14, both architectures | Bare R, native NumPy/SSL, a user-owned SQLite connection through the SQL tool, both interruptions, and restart passed; this used preinstalled packages and did not establish managed preparation or DuckDB support |
| Default managed runtime on Alpine                                                           | Not qualified: `r-lib-ir` 0.4.0 has no matching musllinux distribution; an unmodified native build of its v0.4.0 source succeeded, but default R-package preparation encountered the build failures below          |

The GNU ARM failure reproduces through public R cells with both `serve` and `serve --no-sandbox`.
`stop("portable-error")` also enters the crash handler: R 4.6.1 reports an invalid-permissions segfault and Ubuntu's R 4.3.3 reports an illegal operand.
The affected return path is the current C DLL-REPL boundary in `src/r_repl.c`, which relies on returning through the context installed by `R_ReplDLLinit()` after that function has returned.
Repairing and qualifying that R error/interrupt boundary is separate from companion linkage; this change does not replace the worker's REPL architecture.

A dynamic musl build with `RUSTFLAGS='-C target-feature=-crt-static'` demonstrates that R/Python/SQL loading can work, but that flag alone is not full musl-host support.
Maturin 1.15 repairs its `libgcc_s` dependency by placing the ELF executable under `mcp_console.scripts` in site-packages and installing a Python launcher.
Console's current relocatable bundle contract expects the executable under `bin`, so that repaired musllinux wheel fails companion lookup before a runtime starts.
The native-layout prototype avoids that packaging transformation; it is not a qualified wheel.

On Alpine AArch64 with R 4.5.0/GCC 14.2, default preparation also failed to compile DuckDB 1.5.5, Arrow 25.0.1, and systemfonts with the distribution's LTO/fortify settings: `snprintf` or `vsnprintf` reported `function body can be overwritten at link time`.
The `fs` build additionally required libuv development headers.
These are dependency-build prerequisites and failures, not evidence of a sandbox policy denial.
Managed Python preparation, default DuckDB extension loading, and a relocatable musllinux installation therefore remain unverified as a complete application combination.
No musllinux application wheel is added to the release matrix.

The portable companion improvement is independently usable by the existing GNU wheels.
It removes their companion libcap/OpenSSL runtime requirements without claiming an older GNU application baseline or full musl-host support.
Both musl release jobs passed in the [coordinated runner CI run](https://github.com/t-kalinowski/codex/actions/runs/34474894244), including actual ELF inspection and public transport/lifecycle contracts.
Console's macOS `scripts/check` passed, including source and wheel installation checks; Linux installed-layout checks passed on both architectures.
See [release preparation](../RELEASE.md) for source-build prerequisites, explicit companion target selection, wheel audits, and installed-layout checks.

## Namespace compatibility review

This review starts at MCP Console main `f35a304d` and its actual runner pin `d488fc969da435f93ea5937c7f284fa91a8c2575`.
The coordinated [runner changes](https://github.com/t-kalinowski/codex/compare/d488fc969da435f93ea5937c7f284fa91a8c2575...7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc) advance the pin to `7aacbcf1bca0f173f036617a5ee8ae71e18fb8cc`.
Default execution still uses bubblewrap and the requested filesystem/network policy.
Namespace or policy failure never selects an unrestricted target or a different backend.

| Host condition                                            | Previous integration                                      | Current behavior                                                                |
| --------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Fresh namespace-local procfs                              | Supported                                                 | Supported                                                                       |
| Native procfs preflight selects inherited procfs          | Rejected at the target hook                               | Supported with the same namespace and policy enforcement                        |
| `pidfd_open` unavailable or denied                        | Startup fails                                             | Native control and direct-child waits remain available                          |
| `pidfd_send_signal` unavailable or denied                 | Retirement fails                                          | Native channel requests retirement; completion still requires the child wait    |
| Host subreaper unavailable                                | Startup fails                                             | No host subreaper requested                                                     |
| Host child lists or namespace PID discovery unavailable   | Cleanup depended on host child lists                      | No child-list or `NSpid` discovery                                              |
| No native child completion evidence                       | Could mistake a missing child-list file for an empty list | Nonzero failure and retained private storage                                    |
| Stopped namespace init, pidfds unavailable                | Startup already unsupported                               | Retirement deadline fails explicitly; private storage remains                   |
| Explicit Landlock filesystem/network backend              | Rejected                                                  | Direct exec, native policy checks, no process isolation or supervised lifecycle |
| Unavailable namespace operations with bubblewrap selected | Failure                                                   | Failure; no backend switch                                                      |
| Suitable host helper with a damaged bundle                | Launch rejected                                           | Launch rejected; every bundled file is verified before helper selection         |

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
Console verifies every bundled file before launching the runner.
A missing or modified bundled helper fails immediately, even when a suitable host helper is available.


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
