# macOS sandbox supervision

**Status:** Current implementation

This document describes host-side lifetime ownership for sandboxed worker generations and standalone sandbox commands.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Lifetime ownership

Each sandbox lifetime has one host-side manager outside Seatbelt and one sandbox root in its own process group.
The manager tracks the root and every descendant identity it observes by PID and process start time.
Once observed, a descendant remains a cleanup target after changing process group or session.
The manager also adopts the private temporary-directory guard for the lifetime.

The host owner retains the manager process and the direct sandbox root as waitable children.
After the manager reports readiness, the owner starts monitoring manager exit and transfers its backup temporary-directory guard to that monitor before entering the ownership-commit exchange.
The relay remains a transport and direct-worker owner; it does not own observed-descendant cleanup or the private directory.

## Startup

The host starts the manager before the sandbox root, then sends the root PID, cleanup timeout, and private-directory path over a private control socket.
The manager validates that the root is the owner's direct child, attaches its descendant tracker, adopts the directory guard, and reports readiness.
The host installs manager-failure recovery while the direct root remains live and waitable, then commits primary cleanup ownership and waits for confirmation.

The built-in relay waits at a startup gate until that commit, so it cannot launch the worker before manager observation begins.
A custom relay has no equivalent cooperative gate.
Darwin cannot atomically attach the tracker while spawning the root, so a process that detaches before observation remains outside the implemented guarantee.

## Retirement

Normal restart, automatic replacement, orderly shutdown, relay failure, and abrupt server exit all retire the same manager-owned lifetime before replacement or exit continues.
The manager closes the original root process group as a backstop for a same-group fork that raced observation and retires every observed identity within the configured timeout.
For normal root exit, the host retains the waitable root while observed cleanup finishes and the group backstop runs.
For forced retirement or owner loss, the manager closes the group while the recorded root identity is still available, signals the exact root, and then waits for observed cleanup.

The manager removes the private directory only after successful process cleanup.
It preserves the directory on any cleanup error because a surviving process may still use it.

## Manager failure

If the manager exits unsuccessfully while its host owner still retains a live, waitable root, the owner reconstructs the root's current process tree and performs bounded cleanup before replacement.
The fallback revalidates process identities immediately before signaling and closes the still-pinned process group as a backstop.
It preserves the private directory on failure.

This fallback can recover only descendants still reachable from the root's current ancestry.
It cannot reconstruct a descendant that detached before the manager failed.

## Scope

This ownership applies to worker relay generations and to the standalone `mcp-console sandbox` command.
The standalone launcher also owns terminal foreground-group transfer and direct signal forwarding while the command runs.
Linux and Windows are not supported.
