# macOS sandbox supervision

**Status:** Current implementation

This document describes host-side lifetime ownership for sandboxed worker generations.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Normal generation ownership

The server retains the direct `sandbox-exec` relay child and starts a dedicated observer after spawn.
The observer owns a Darwin `kqueue`, records descendants by PID and process start time, and consumes fork and exit events for the lifetime of the generation.
Once observed, a descendant remains a cleanup target after changing process group or session.

At restart, replacement, failure, or shutdown, the server wakes that observer through an `EVFILT_USER` event.
Retirement signals every observed identity, closes the relay's original process group as a race backstop when the caller requested one, and reaps the direct relay last.
Any cleanup error preserves the private temporary directory because an unobserved process may still use it.

Darwin cannot atomically attach an observer while spawning.
A process that becomes orphaned before the root watch or a corresponding fork event is observed remains outside the implemented guarantee.

## Committed crash ownership

After the server observer has attached, the server starts one hidden `sandbox-manager` process for that sandbox lifetime.
Its initialization travels on manager fd 0; all other nonstandard descriptors are closed at exec.
Before it reports readiness, the manager independently:

- validates the sandbox root identity and its direct-child relationship to the server;
- attaches its own PID-and-start-time descendant tracker;
- registers an event-driven watch for server exit; and
- adopts the private temporary-directory path after validating its owner prefix and canonical temporary-directory parent.

Readiness commits crash-independent ownership.
If the server then exits without running normal shutdown, the manager's owner watch wakes, retires its observed process tree, and attempts to remove the private temporary directory after successful cleanup.
It preserves the directory on a cleanup error because an unobserved process may still use it.
With no surviving server to receive a filesystem error, directory removal itself is best effort and can leave the directory behind.
The manager signals only recorded PID-and-start-time identities.
It does not signal the relay's raw process-group ID after server loss because no waitable server child remains to pin that group against reuse.

The manager cannot recover a descendant that becomes orphaned before its tracker observes the process, even if it later reports readiness.
The server's earlier observer can still own such a process during normal retirement, but that local ownership disappears with an abrupt server exit.
An uncatchable server failure before manager readiness can also preempt manager commitment.
Darwin does not provide an atomic spawn-and-observe operation that closes either interval.

When the relay root exits while the server remains live, the manager retires its observed descendants without removing the private directory.
It keeps crash ownership until the server sends a final directory disposition.
The server marks the start of normal retirement before local cleanup, waits for the manager to report that its observed identities are retired, and reaps the direct relay before committing the final directory disposition.
It removes the directory after success or preserves it after an error.
If the server exits after marking retirement but before the final disposition, the manager conservatively preserves the directory.
If manager control closes without that marker, the manager treats the loss as an abrupt server exit and attempts removal after cleanup.

## Manager failure

The server retains a blocking monitor for the manager process.
An unexpected manager exit is treated as a generation failure: the monitor signals the exact relay identity.
The existing local observer remains alive and completes retirement of the observed tree, including descendants that moved beyond the relay's group, before the normal server path closes the still-pinned process group as its race backstop.
This keeps manager failure recovery within the server while the server itself is available.

A manager that survives the server is self-contained.
It uses no server thread, relay protocol message, or sandboxed code to complete cleanup.
After its observed generation retires, it remains available until the server completes the normal disposition handoff or control closes with the server.

## Scope

This ownership applies to `SandboxedCommand::spawn`, which is used for worker relay generations.
The standalone `mcp-console sandbox` status path keeps its existing launcher-owned observer and does not gain crash-independent or terminal job-control semantics here.
