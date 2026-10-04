# Sandbox integration

Console selects application policy and verifies the private native executable; the runner owns OS enforcement, signals/terminals, descendant retirement, and private storage.
Console does not implement a second native supervisor or recovery monitor.
[`sandbox-runner.json`](../sandbox-runner.json) pins the runner and its protocol; its executable contract/tests define native behavior.

This page covers `sandbox.provider: native`, the default for local, SSH, and ordinary Docker targets.
[Docker Sandbox](DOCKER_SANDBOX.md) instead uses compute provider enforcement and never invokes the native companion.

## Application policy and launch

```text
caller / MCP server
└─ mcp-console sandbox → verified native runner
   └─ native enforcement and target
      └─ relay → worker, or a standalone command
```

On Unix, exec preserves the frontend PID and caller.
Windows uses a waiting frontend; the runner observes both its frontend and the session owner with process handles.
Console passes one immutable JSON policy through `--config-env MCP_CONSOLE_SANDBOX_CONFIG -- COMMAND...`; the runner consumes and removes that variable.
Arguments, cwd, environment, and fd 0/1/2 remain ordinary launch inputs.
There is no policy file or stdin handoff.
The native runner owns enforcement and process cleanup.

Without a selected profile, Console requests host reads, restricted networking without a proxy, and private writable storage exported as `TMPDIR` (also `TEMP` and `TMP` on Windows).
macOS adds its trusted policy extension.
The runner creates/owns `sandbox-XXXXXX/data`.
Additional writable paths are persistent user data and are never deleted on retirement.
Console preserves `MCP_CONSOLE_SANDBOX=1` and removes `DYLD_INSERT_LIBRARIES` / `LD_PRELOAD` from the trusted frontend handoff.

Configuration is captured once, including absence of a file.
Restarts cannot rediscover changed YAML.
`:workspace` adds workspace writes with metadata protections; these are overridable defaults, not denial ceilings.
See [configuration](SANDBOX_CONFIGURATION.md) for composition and complete policies.

For SSH, the controller captures policy but the execution host materializes it, runs native preflight, and owns enforcement/proxy/storage.
Docker materializes native policy in its owned container.
Controller paths and proxy addresses must not be substituted for execution-host paths/addresses.

The server owns one ordinary launcher child per generation.
It requests relay shutdown and reaps the child before joining old I/O and allowing replacement.
Unix can request runner retirement through SIGTERM; Windows keeps its waiting frontend alive until the native runner reports confirmed Job cleanup.
Cancellation before ready uses the same retirement path, not an early SIGKILL that bypasses cleanup.
Logical generation retirement and successful native cleanup are distinct facts.

Installation-relative provenance/digest/license checks fail before execution; there is no runtime download, extraction, or runner PATH search.
Linux may use a trusted host bwrap; a selected bundled helper is verified through the same open descriptor used for execution.
See [packaging](../RELEASE.md#private-sandbox-executable).

## Supported hosts and lifetime limits

**macOS:** Seatbelt enforces policy.
The target leads an owned process group; the runner remains outside it, retaining a waitable root through retirement.
It retires that group and detached descendants it observed.
A descendant that detaches and becomes orphaned before observation may escape discovery; identity checking and signaling are not atomic.
Exclusive foreground-terminal ownership is transferred/restored; with a foreground peer, the caller group stays in control and signals are forwarded.
General shell job suspension/resumption is unsupported.

**Linux:** The default native path uses bubblewrap and namespace retirement.
The runner requires native child-completion evidence, not host subreaper, child-list, or namespace-PID inference.
Fresh procfs is optional; inherited procfs preserves namespaces/policy but can expose host PIDs and readable metadata.
Pidfds are an optional emergency mechanism; without them a stopped namespace init can exceed retirement's deadline.
Missing wait evidence means failure and retained storage, not successful cleanup.
See [Linux compatibility](LINUX_COMPATIBILITY.md).

Configured caller death triggers retirement **while the runner lives**.
Owned launches with nonterminal stdin/stdout isolate the runner's process group so caller-group death does not kill the cleanup owner.
Signal the runner to interrupt its workload independently of the caller.

The runner has no independent recovery process.
Linux parent-death links terminate the workload after native readiness, but not throughout every earlier setup window.
macOS does not guarantee descendant termination after runner death.
Neither platform guarantees storage deletion or terminal restoration after runner death, nor recovery from a stopped/hung runner.
Surviving processes retain native enforcement.
A final forced launcher kill cannot establish successful cleanup.

Cleanup failures are nonzero errors with diagnostics; unproven retirement retains private storage.
SSH requires remote cleanup acknowledgment, not just SSH exit; undetected network partitions have no lease deadline.
Provider removal has its own receipts.
**Windows x64:** The elevated native backend uses Console-specific sandbox accounts, filesystem ACLs, network rules, and a non-breakaway Job.
Account provisioning is an explicit interactive setup operation.
Restricted-token execution is opt-in and requires enabled networking and host reads.
Both modes confirm Job retirement before returning, and private storage is removed only after confirmation.
Forced frontend exit is not a cleanup receipt.
See [Windows setup, validation, and limits](WINDOWS.md).
Other operating systems are unsupported.

Unrestricted, external, and explicit Landlock modes have different guarantees; read [enforcement modes](SANDBOX_CONFIGURATION.md#filesystem-and-enforcement-modes).
Local/SSH `--no-sandbox` removes native enforcement and descendant cleanup; normal relay shutdown still reaps the direct worker.
Docker/SBX outer resources remain.

## Policy extensions and compatibility

[`policy_extensions.sbpl`](../src/sandbox/policy_extensions.sbpl) owns each Console-specific macOS allowance and its rationale.
Native base policy supplies many runtime allowances already; do not duplicate them locally or infer that removing a redundant rule means the workflow no longer needs the permission.

The important local boundary is host-terminal protection: host terminal reads and mutating ioctl remain denied, while PTYs created inside the sandbox support processx and other interactive children.
Private temporary storage is disposable and fully mutable, including its metadata paths; it is not a protected workspace.

Evidence for exceptions differs.
`kern.boottime` is required by the tested ps / processx path.
Several uv/SystemConfiguration, Quarto/sysctl, pointer-authentication, and device exceptions retain historical observations without a confirmed current fatal dependency.
The comments state uncertainty; do not assign unsupported callers or delete a rule solely because a current test passes without exercising its motivating workflow.

To remove an exception as redundant, inspect the pinned base.
To remove it as unnecessary, reproduce its actual sandboxed workflow without it across relevant platform/runtime versions.
Host resolver tests cannot establish worker sandbox compatibility.
Public CLI sandbox, processx, parallel Python, Quarto, and offline uv cases are the regression evidence; historical test counts are not current acceptance results.

Native enforcement does not make the whole application safe for hostile input: submitted code is shell-class capability and host reads can expose secrets.
[Dependency preparation](RESOLVER.md) uses a separate native policy on macOS and Linux; its host reads and shared cache writes still require trusted package sources.
