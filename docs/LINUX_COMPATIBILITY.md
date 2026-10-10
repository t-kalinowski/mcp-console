# Linux host compatibility

Linux compatibility depends on effective permissions and host facilities, not only a kernel version.
[`sandbox-runner.json`](../sandbox-runner.json) selects the native implementation; [sandbox configuration](SANDBOX_CONFIGURATION.md) selects application policy.

## Policy and backend contract

Default execution uses bubblewrap with user, mount, and PID namespaces, requested filesystem/network rules, and supervised retirement.
It needs procfs, permitted namespace setup, and the requested native/seccomp facilities.

There is no automatic Landlock or unsandboxed fallback.
Standalone `linux_backend: landlock` is rejected.
A namespace failure remains a launch failure.

Fresh namespace-local procfs is optional.
An inherited-procfs alternative keeps the backend and policy but can reveal host PIDs and permitted metadata.
Pidfds are an optional emergency mechanism; without them, a stopped namespace init can exceed cleanup deadlines.
Missing native wait evidence causes failure and retained storage, not inferred cleanup success.

### Filesystem limits

Native classification uses effective permissions, not only a kind label.
Full-write policies do not become restricted because their entries contain ineffective carve-outs.
`unrestricted` ignores narrowing entries; `external-sandbox` without a proxy delegates both filesystem and networking enforcement to an outer boundary.

Mount masking can hide a narrower read beneath a broader denial or make deeper denials fail setup.
Individual-file write roots can be rejected, and missing write roots are skipped until another launch.
Create intended writable directories first.
Console does not create permanent placeholders, grant parents, or switch backends to work around native limits.

[Sandbox configuration](SANDBOX_CONFIGURATION.md#filesystem) owns path precedence and metadata protection.
Restricted-policy security results must not be attributed to unrestricted or externally enforced workloads.

## Host and container setup

Ubuntu AppArmor policy can permit system `/usr/bin/bwrap` while denying a relocated bundled helper.
Check native diagnostics and kernel policy records; a working system helper does not validate the bundle.

Ordinary containers may deny nested namespace setup.
Console adds no privileged flags or implicit policy changes.
Deliberately selecting external mode delegates enforcement to the container and has different cleanup guarantees.

A trusted host helper can take precedence.
When the bundled helper is selected, its digest is verified and it is executed through the verified descriptor.
Test that path in an approved disposable environment; do not weaken a user's host security policy merely to pass a check.

## Reproducing the comparisons

```sh
scripts/test cli/sandbox/test_procfs
scripts/test cli/sandbox/test_configuration
python3 tests/sandbox_installation.py target/release/mcp-console
```

The procfs matrix compares fresh/inherited views with readable/denied sentinel paths and restricted/enabled networking.
It checks direct policy behavior and attempts through process metadata, descriptors, memory, signals, and namespace entry.
Host restrictions must not substitute for the sandbox property under test.

Namespace-link access and namespace entry are separate assertions, resolving [SP-5](https://github.com/t-kalinowski/mcp-console/issues/603).
The trusted fixture driver first proves that it can enter its disposable network namespace, then supplies a validated handle to the sandboxed target.
The disposable host process drops the driver's capabilities so they cannot conceal a ptrace bypass.
This keeps namespace entry reachable even when procfs denies access to the host process's namespace link, and retains exact errors for both checks.

These probes establish specific contracts, not proof against all kernel attacks.
Fixture process-observation requirements are separate from production requirements.
[Boundary tests](../tests/boundaries/README.md) covers capability skips and safe namespace-PID handling.

Release acceptance additionally checks the installed bundle with an empty PATH and native floor-runtime images.
See [Release](../RELEASE.md#linux-floor-validation); an ABI report, successful normalization, or skipped native probe is not a sandbox acceptance result.
