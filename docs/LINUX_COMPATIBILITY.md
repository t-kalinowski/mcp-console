# Linux host compatibility

Compatibility depends on effective permissions and host facilities, not just a kernel version.
[`sandbox-runner.json`](../sandbox-runner.json) selects the native implementation; [sandbox configuration](SANDBOX_CONFIGURATION.md) defines Console's policy choices.
A previously passing host/pin is not acceptance of every revision.

## Policy and backend contract

Default native execution uses bubblewrap, user/mount/PID namespaces, requested filesystem/network enforcement, and supervised retirement.
It requires procfs, permitted namespace setup, and the requested native/seccomp capabilities.
It does not require host subreaper, child-list, or namespace-PID discovery.

Fresh namespace-local procfs is optional.
The native inherited-procfs alternative retains isolation/policy but can reveal host PIDs and permitted metadata.
It is not a backend switch.
Pidfds are optional emergency termination; without them, a stopped namespace init can exceed the cleanup deadline.
Missing native wait evidence causes failure and retained storage, never inferred successful cleanup.
Resolver child observation uses SIGCHLD notifications and non-reaping `waitid`, without requiring pidfds.
Its observer unblocks SIGCHLD on its own thread, retains an independent cancellation endpoint, and unregisters the notification before joining; cancellation is not evidence of child exit.

The standalone Landlock backend has been removed; `linux_backend: landlock` is rejected before native setup.
There is no automatic Landlock or unsandboxed fallback.

### Filesystem classification

Native classification uses effective permissions, not only the `kind` string:

| Policy                                               | Result                                                                                     |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `unrestricted`                                       | Full filesystem access, native supervision, selected networking. Entries do not narrow it. |
| `external-sandbox`, no proxy                         | Delegated filesystem/network enforcement; ordinary process-group supervision.              |
| Restricted special-root write, no effective carveout | Full-write classification with native supervision.                                         |
| Root write with effective read/deny carveouts        | Restricted policy subject to native mount validation.                                      |
| Root read with writable directories                  | Restricted policy subject to writable-root/host prerequisites.                             |

At equal targets, write overrides read and deny overrides both; narrower paths have their native precedence.
A literal `/` path is not the special root entry and undergoes writable-root preparation.
Classification is not proof setup can succeed: protected metadata mount targets can be absent or inaccessible.

The mount backend cannot fully reopen a narrower read beneath a broader denial; an ancestor mask may hide it and a deeper denial may fail setup.
Existing file write roots also have a native limitation; missing write roots are skipped until a later launch.
Console does not grant parents, create permanent placeholders, or switch backends to work around these outcomes.
See [writable paths](SANDBOX_CONFIGURATION.md#filesystem).

Full-write modes can influence shared files and unsandboxed processes.
Restricted- policy security results must not be attributed to those modes.
External mode without a proxy adds no networking restriction, even with `network: restricted`.

## Host and container setup

Ubuntu AppArmor policy may allow a system `/usr/bin/bwrap` while denying namespace operations to a relocated bundled helper.
Check native diagnostics and kernel AppArmor records; do not infer bundle support from a working system helper.
Missing namespace permission remains a launch error, not a reason to retry with weaker isolation.

Ordinary Docker containers may deny nested namespace operations.
Console adds no privileged flags or implicit backend changes.
Deliberately choosing external mode delegates enforcement to the container, with its different guarantees.
Use a namespace-capable disposable environment for bundled-helper and nested- procfs tests rather than changing a user's host security policy.

A suitable trusted host helper takes precedence; a damaged unused bundle does not block it.
A selected bundled helper must match its embedded digest and is executed through that same verified descriptor.
[Release checks](../RELEASE.md) exercise the bundled path with an empty PATH and validate actual ELF dependencies.

## Reproducing the comparisons

```sh
scripts/test cli/sandbox/test_procfs
scripts/test cli/sandbox/test_configuration
python3 tests/sandbox_installation.py target/release/mcp-console
```

The procfs fixtures use disposable same-user processes, synthetic data, an explicitly ptraceable host process, control pipes, and a loopback listener.
The matrix combines fresh/inherited procfs with readable/denied sentinel paths and restricted/enabled networking.
It checks direct policy behavior and attempts through host environ/root/cwd/fds/memory, signals, ptrace, process-memory syscalls, and network-namespace entry.
Host restrictions must not substitute for the sandbox property under test.

Runner contracts additionally deny pidfd/subreaper syscalls, withhold native wait status, stop namespace init, and reject unsupported policies/backends.
These establish specific interfaces and failure behavior, not a general proof against all kernel attacks.
Test fixture process-observation requirements are separate from production requirements.
See the [boundary guide](../tests/boundaries/README.md) for execution modes, skips, and safe namespace-PID handling.
