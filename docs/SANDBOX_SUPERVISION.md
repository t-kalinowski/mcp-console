# macOS sandbox supervision

**Status:** Current implementation

This document describes host-side lifetime ownership for sandboxed worker generations and the standalone `mcp-console sandbox` command.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Normal generation ownership

The server asks `sandbox-exec` to run a hidden wrapper that blocks on a private inherited release channel before executing either the built-in or a configured custom relay.
The server retains that direct root and starts a dedicated observer after spawn.
The observer owns a Darwin `kqueue`, records descendants by PID and process start time, and consumes fork and exit events for the lifetime of the generation.
Once observed, a descendant remains a cleanup target after changing process group or session.

While the root is still blocked, the server starts the crash manager and waits for its independent observer and temporary-directory adoption to report readiness.
The server then sends one release byte; the wrapper closes the channel and replaces itself with the configured relay in the same process identity.
Relay and worker code therefore cannot run before both host observers are attached.

At restart, replacement, failure, or shutdown, the server wakes its observer through an `EVFILT_USER` event.
Retirement signals every observed identity, closes the relay's original process group as a race backstop when the caller requested one, and reaps the direct relay last.
Any cleanup error preserves the private temporary directory because an unobserved process may still use it.

Darwin still cannot resolve every later fork atomically.
A descendant that becomes orphaned before a corresponding fork event is observed remains outside the implemented guarantee.

## Standalone normal ownership

The standalone launcher asks `sandbox-exec` to run the same hidden wrapper, blocked on a separate private inherited release channel before executing the requested command.
While that same direct root is blocked, the launcher attaches a descendant tracker, starts the manager, and waits for it to adopt the private temporary directory and report readiness.
The launcher then sends one release byte; the wrapper closes the channel and replaces itself with the requested command.
It blocks on that tracker until the direct root exits, then marks the start of normal retirement before the first termination pass.
The launcher retires every identity its tracker observed, waits for the manager's independent cleanup acknowledgement, reaps the direct root, and commits the final remove-or-preserve directory disposition.
The direct command's exit status remains the standalone command's exit status when cleanup succeeds.

The launcher remains the authority for normal cleanup.
The manager does not replace its tracker, decide the command's exit status, or add terminal job-control and signal-relay semantics.

## Committed crash ownership

After the normal owner observer has attached, the server or standalone launcher starts one hidden `sandbox-manager` process for that sandbox lifetime.
Its initialization travels on manager fd 0; all other nonstandard descriptors are closed at exec.
Before it reports readiness, the manager independently:

- validates the sandbox root identity and its direct-child relationship to the owner;
- attaches its own PID-and-start-time descendant tracker;
- registers an event-driven watch for owner exit; and
- adopts the private temporary-directory path after validating its owner prefix and canonical temporary-directory parent.

Readiness commits crash-independent ownership.
If the owner then exits without running normal shutdown, the manager's owner watch wakes, retires its observed process tree, and attempts to remove the private temporary directory after successful cleanup.
It preserves the directory on a cleanup error because an unobserved process may still use it.
With no surviving owner to receive a filesystem error, directory removal itself is best effort and can leave the directory behind.
The manager signals only recorded PID-and-start-time identities.
It does not signal the root's raw process-group ID after owner loss because no waitable owner child remains to pin that group against reuse.

The manager cannot recover a later descendant that becomes orphaned before its tracker observes the process, even if it previously reported readiness.
The owner's observer can still own such a process during normal retirement, but that local ownership disappears with an abrupt owner exit.
An uncatchable owner failure before manager readiness can also preempt manager commitment.
In both worker and standalone paths, closing the private startup channel prevents configured sandbox code from running before commitment; a pre-readiness crash can still leave the owner-created temporary directory behind.

When the root exits while the owner remains live, the manager retires its observed descendants without removing the private directory.
It keeps crash ownership until the owner sends a final directory disposition.
The owner marks the start of normal retirement before local cleanup, waits for the manager to report that its observed identities are retired, and reaps the direct root before committing the final directory disposition.
It removes the directory after success or preserves it after an error.
If the owner exits after marking retirement but before the final disposition, the manager conservatively preserves the directory.
If manager control closes without that marker, the manager treats the loss as an abrupt owner exit and attempts removal after cleanup.

## Manager failure

Each normal owner retains a blocking monitor for the manager process.
An unexpected manager signal is treated as a sandbox-lifetime failure: every monitor signals the exact root identity.
For worker generations, the server observer remains alive and completes retirement of the observed tree before the normal server path closes the still-pinned process group as its race backstop.
For a standalone command, the monitor also wakes the launcher tracker so it starts retirement even if the root signal fails; otherwise the tracker sees root exit and returns the root's signal-derived exit status after cleanup.
In both paths, successful local recovery removes the owner-held private temporary directory.

A manager that survives its owner is self-contained.
It uses no owner thread, relay protocol message, or sandboxed code to complete cleanup.
After its observed lifetime retires, it remains available until the owner completes the normal disposition handoff or control closes with the owner.

## Scope

This ownership applies to `SandboxedCommand::spawn`, which is used for built-in and custom worker relay generations, and to `SandboxedCommand::status`, which implements `mcp-console sandbox`.
The worker path retains its server-owned process-group race backstop and gates the relay before either relay implementation runs.
The standalone path retains inherited standard streams and does not add terminal job-control or signal-relay semantics.
