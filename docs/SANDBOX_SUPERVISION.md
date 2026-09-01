# macOS sandbox supervision

**Status:** Current implementation

This document describes host-side lifetime ownership for sandboxed worker generations and the standalone `mcp-console sandbox` command.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Lifetime ownership

Each sandbox lifetime has one host-side manager outside Seatbelt and one direct sandbox root in its own process group.
The manager records the root and every descendant identity it observes by PID and process start time.
Once observed, a descendant remains a cleanup target after changing process group or session.
The manager also adopts the private temporary-directory guard for the lifetime.

The host owner retains the manager process and direct sandbox root as waitable children.
After the manager reports readiness, the owner starts monitoring manager exit and transfers its backup directory guard to that monitor before entering the ownership-commit exchange.
The relay remains a transport and direct-worker owner; it does not own observed-descendant cleanup or the private directory.

## Startup

The host starts the manager before the sandbox root, then sends the owner PID, root PID, cleanup timeout, and private-directory path over a private inherited Unix socket.
The manager validates the direct-child relationship and exact root identity, attaches its descendant tracker, adopts the directory guard, and reports readiness.
The host installs manager-failure recovery while the direct root remains live and waitable, then commits primary cleanup ownership and waits for confirmation.

The built-in relay waits at a startup gate until that commit, so it cannot launch the worker before manager observation begins.
A custom relay has no equivalent cooperative gate.
The standalone launcher instead asks `sandbox-exec` to run a hidden wrapper blocked on a private release channel.
It releases that same root into the requested command only after manager ownership is committed.
Abrupt owner loss before readiness or commitment can preempt manager adoption.
The standalone gate prevents requested command code from running in that interval, but private-directory cleanup is not guaranteed.

Darwin cannot atomically attach a tracker while spawning.
A process that becomes orphaned before the root watch or corresponding fork event is observed remains outside the implemented guarantee.
The standalone startup gate prevents requested command code from creating such a process before initial manager observation.

## Retirement

Normal restart, automatic replacement, orderly shutdown, relay failure, and abrupt owner exit all retire the same manager-owned lifetime.
The manager closes the original root process group as a race backstop and retires every observed identity within the configured timeout.
During owner-controlled retirement, the host retains the waitable root through cleanup and reaps it last.

For normal standalone root exit, the launcher marks retirement on the manager control stream.
The manager finishes observed cleanup, reports completion, and waits for the launcher's final remove-or-preserve directory disposition.
The launcher preserves the directory if that acknowledgement times out, while still returning the direct command's exit status after process cleanup succeeds.
If the launcher exits after marking retirement but before sending the final disposition, the manager conservatively preserves the directory.
Control closure before the marker is an abrupt owner exit: the manager stops the lifetime and removes the directory after successful cleanup.

The manager preserves the private directory on any cleanup error because a surviving process may still use it.

## Manager failure

Each host owner retains a blocking monitor for the manager process.
If the manager exits unsuccessfully while the owner still retains a live, waitable root, the monitor reconstructs the root's current process tree and performs bounded cleanup before the owner continues.
The fallback revalidates process identities immediately before signaling and closes the still-pinned process group as a race backstop.
It preserves the private directory on failure.
If manager completion times out, the owner requests forced exit and allows one more bounded recovery interval.
If the manager still does not report completion, the owner returns an error without joining the live monitor thread; that monitor retains and preserves the backup directory guard.

This fallback can recover only descendants still reachable from the root's current ancestry.
It cannot reconstruct a descendant that detached before the manager failed.
For a standalone command, successful fallback preserves the root's signal-derived exit status; a fallback error wakes the launcher with an error and leaves the directory in place.

## Standalone job control

The standalone launcher gives the requested command its own process group and transfers foreground-terminal ownership before exec.
Terminal-generated signals therefore reach the command group directly.
`SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` addressed to the launcher are blocked, consumed synchronously, and relayed once to that group.
After root exit, the launcher restores its own foreground group before returning the command status.

## Scope

This ownership applies to `SandboxedCommand::spawn`, which is used for worker relay generations, and to `SandboxedCommand::status`, which implements `mcp-console sandbox`.
Linux and Windows are not supported.
