# Linux sandboxing

`mcp-console serve` and `mcp-console sandbox -- COMMAND` use the pinned private native runner on Linux.
`serve --no-sandbox` explicitly bypasses the policy, private temporary directory, and descendant cleanup.
Startup failures never select an unsandboxed fallback.

## Policy and prerequisites

The native runner uses bubblewrap, user and mount namespaces, a PID namespace, restricted networking, and seccomp.
It requires Linux 5.11 or later, mounted `/proc`, UID/GID mapping, and host permission to create user, mount, PID, and network namespaces.
AppArmor and container policies can prohibit these operations independently of kernel support.
Before executing submitted code, the target wrapper verifies that `/proc` belongs to the new PID namespace.
Hosts that prohibit a fresh procfs mount fail startup even if the native helper can otherwise start with inherited procfs.
A suitable system `bwrap` from the trusted startup `PATH` takes precedence over the bundled helper beside the private runner.
The bundled helper requires `libcap.so.2`.

The host filesystem is readable and mounted read-only, with a writable bind mount for one private temporary directory supplied as `TMPDIR`.
Host writes fail with `EROFS`.
The native policy also protects metadata anchors at writable roots; the temporary root itself is a mount point and cannot be removed or renamed from inside the sandbox.
Ordinary nested temporary files and directories remain writable.
Host process IDs are hidden by the PID namespace; IDs printed by evaluated code are namespace-local.
The target cannot create IP sockets or connect, bind, or listen on sockets.
Local Unix socket pairs support interpreter IPC and the relay-worker sideband without granting access to host services.
R, Python, and DuckDB resolution still runs on the host and returns readable installed paths to the sandbox.

## Ownership and startup

The single-threaded sandbox CLI registers as a child subreaper and forks a host manager before starting runtime threads.
The manager also registers as a subreaper and directly owns the private runner.
Its control socket remains outside the sandbox.
The manager prepares the same protocol-2 bootstrap pipe used on macOS and writes the frame through a blocking descriptor wait that also observes cancellation and runner exit.
No target can start before that frame is sent.

In parent-owned mode, the launcher validates `--exit-with-parent` against its current parent, opens a pidfd, and rechecks parenthood before launch.
Parent exit closes manager control and requests retirement.
Manager-control EOF also requests retirement if the launcher crashes.
The launcher and manager relinquish their copies of target stdin after transferring it to the runner.
The runner retains the target's original stdin description and closes its own copy after launch.

## Retirement and failure

The manager kills its direct children, observes exit through pidfds, and reaps them.
Kernel child adoption supplies any remaining detached descendants as the next batch.
Cleanup repeats until no children remain, with a one-second deadline.
This also covers processes that have created new sessions or whose original parents already exited.
The native PID namespace independently terminates its processes when its init exits.

After successful process cleanup, directory removal is best effort.
Normal target completion preserves its exit status; signals map to `128 + signal`.
An owned SIGTERM request or owner exit returns success after cleanup.
A manager that does not acknowledge retirement within two seconds is killed and cleanup is recovered by the launcher.
If the manager crashes, the launcher adopts its children, performs the same bounded cleanup, removes unused temporary storage, and reports the manager's status.
The runner also receives SIGKILL on manager death.
If cleanup cannot prove all children exited, the directory is preserved and retirement fails.
Simultaneous loss of both host owners has no cleanup guarantee.

## Signals and terminals

The launcher consumes SIGHUP, SIGINT, SIGQUIT, and SIGTERM through signalfd.
Ordinary signals travel over private manager control to the native namespace init, which forwards them to the target.
Owned SIGTERM requests retirement instead.
The hidden target wrapper restores inherited ignored signal dispositions and then the original signal mask before exec.
Supervisors reset ignored SIGCHLD and SIGTERM for their own child reaping and retirement; those changes do not reach the requested command.
Signals arriving before native namespace creation have no target and are not replayed.

The launcher keeps foreground terminal ownership and forwards terminal-generated signals; native sandbox setup creates a separate target session.
Standard streams retain their original pipe, file, or terminal descriptions.
Stopped/continued shell job control is unsupported.
The native Linux policy does not provide the macOS Seatbelt restriction on mutating an inherited terminal through ioctl; terminal access is part of the caller-supplied standard streams.
EOF and descendant cleanup remain independent of interpreter behavior.
