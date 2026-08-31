# macOS sandbox supervision

**Status:** Current implementation

This document describes host-side lifetime ownership for sandboxed worker
generations. The broader process and responsibility model remains in
[implemented architecture](ARCHITECTURE.md).

## Normal generation ownership

The server retains the direct `sandbox-exec` relay child and starts a dedicated
observer after spawn. The observer owns a Darwin `kqueue`, records descendants by
PID and process start time, and consumes fork and exit events for the lifetime of
the generation. Once observed, a descendant remains a cleanup target after
changing process group or session.

At restart, replacement, failure, or shutdown, the server wakes that observer
through an `EVFILT_USER` event. Retirement signals every observed identity,
closes the relay's original process group as a race backstop when the caller
requested one, and reaps the direct relay last. Any cleanup error preserves the
private temporary directory because an unobserved process may still use it.

Darwin cannot atomically attach an observer while spawning. A process that
becomes orphaned before the root watch or a corresponding fork event is observed
remains outside the implemented guarantee.

## Committed crash ownership

After the server observer has attached, the server starts one hidden
`sandbox-manager` process for that sandbox lifetime. Its initialization travels
on manager fd 0; all other nonstandard descriptors are closed at exec. Before it
reports readiness, the manager independently:

- validates that the sandbox root is a live direct child of the server;
- attaches its own PID-and-start-time descendant tracker;
- registers an event-driven watch for server exit; and
- adopts the private temporary-directory path after validating its owner prefix
  and canonical temporary-directory parent.

Readiness commits crash-independent ownership. If the server then exits without
running normal shutdown, the manager's owner watch wakes, the manager retires its
observed process tree, applies the same optional process-group backstop, and
removes the private temporary directory.

The relay-spawn-to-manager-readiness interval is intentionally not covered in
this slice. The server still owns local cleanup during a manager startup failure,
but an uncatchable server failure in that narrow interval can preempt both
in-process retirement and manager commitment.

## Manager failure

The server retains a blocking monitor for the manager process. An unexpected
manager exit is treated as a generation failure: the monitor signals the exact
relay identity and its dedicated process group when present. The existing local
observer remains alive and completes retirement of descendants that moved beyond
that group. This keeps manager failure recovery within the server while the
server itself is available.

A manager that survives the server is self-contained. It uses no server thread,
relay protocol message, or sandboxed code to complete cleanup. The manager exits
only after its observed generation has retired or after it has detected loss of
its server owner.

## Scope

This ownership applies to `SandboxedCommand::spawn`, which is used for worker
relay generations. The standalone `mcp-console sandbox` status path keeps its
existing launcher-owned observer and does not gain crash-independent or terminal
job-control semantics here.
