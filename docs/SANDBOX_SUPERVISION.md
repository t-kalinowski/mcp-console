# macOS sandbox supervision

**Status:** Current implementation

This document describes the `mcp-console sandbox` launcher's host-side lifetime ownership for sandboxed worker generations and direct commands.
The broader process and responsibility model remains in [implemented architecture](ARCHITECTURE.md).

## Lifetime ownership

Each sandbox lifetime has one host-side manager outside Seatbelt and one direct sandbox root in its own process group.
The direct root initially runs a hidden wrapper blocked on a private inherited release channel before executing the built-in relay, a configured relay, or the standalone command.
The manager records the root and every descendant identity it observes by PID and process start time.
Once observed, a descendant remains a cleanup target after changing process group or session.
The manager also adopts the private temporary-directory guard for the lifetime.

The sandbox launcher is the sole host-side owner and retains the manager process and direct sandbox root as waitable children.
After the manager reports readiness, the launcher starts monitoring manager exit and relinquishes its duplicate temporary-directory guard.
The manager's adopted guard is then the only directory-cleanup owner; launcher-side fallback retains no directory-cleanup state.
After readiness, the launcher holds the control socket open only as the live-sandbox ownership token.
The launcher supports a hidden `--exit-with-parent <PID>` mode, which the server uses for one launcher subprocess per worker generation.
It verifies and captures its exact parent identity before creating the sandbox, watches that identity for exit, and revalidates it after watch registration and immediately before releasing the target.
The server owns only the launcher's piped input and output, inherited error stream, and normal child exit, signaling, and reaping.
The relay remains a transport and direct-worker owner, including local same-group cleanup; it does not own observed-descendant cleanup across process groups or sessions, or the private directory.

## Startup

The launcher starts the sandbox root behind its private gate, then launches the manager with the root PID, cleanup timeout, and private-directory path as native command arguments.
The manager derives the owner PID from its parent, while the private inherited Unix socket carries readiness and then remains open as the ownership token.
The manager validates the direct-child relationship and exact root identity, installs root and descendant tracking plus control-socket observation, adopts the directory guard, and reports readiness.
After receiving readiness, the launcher installs manager-failure recovery while the direct root remains live and waitable, relinquishes its duplicate guard, and writes one release byte.
The hidden wrapper closes the channel and replaces itself with the configured relay or requested command in the same process identity.
Configured sandbox code therefore cannot run before manager observation is active and failure recovery is installed.
The manager control socket carries no messages after readiness; it remains open only as an ownership token, and owner EOF requests retirement.
Abrupt owner loss before readiness closes the control socket and startup gate before configured code runs, but private-directory cleanup is not guaranteed.
Before readiness, the launcher retains its guard and preserves it whenever manager adoption is ambiguous.
After readiness, the launcher relinquishes that guard and the manager becomes the sole directory-cleanup owner.
The adopted guard preserves on unexpected unwind and is armed for removal only after the manager proves cleanup.

Darwin cannot resolve every later fork atomically.
A descendant that becomes orphaned before the manager resolves its fork event remains outside the implemented guarantee.

The requested target runs in a dedicated process group.
Its root waiter blocks in `kevent()` for direct-root exit, signals addressed to the launcher, and the configured parent identity in owned mode.
The ordinary launcher consumes pending `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` and relays them to the target group.
In owned mode, parent exit or launcher-addressed `SIGTERM` requests managed retirement instead; the other supported signals retain their relay behavior.
When the launcher exclusively owns its foreground process group, it transfers controlling-terminal ownership to the target group; when a pipeline peer shares that group, it leaves terminal ownership unchanged.
The manager owns descendant cleanup and the private directory; the launcher owns the direct command's exit status, terminal state, and signal relay.
Stopped/continued job state and general shell-pipeline job control remain unsupported.

## Retirement

Normal restart, automatic replacement, orderly shutdown, relay failure, and abrupt server exit all retire the same launcher-owned lifetime.
For a worker generation, the server first requests graceful shutdown through the relay protocol.
If the relay misses its deadline, the server sends `SIGTERM` to the launcher; owned mode interprets it as a managed-retirement request and remains alive through cleanup.
The manager closes the original root process group as a race backstop and retires every observed identity within the configured timeout.
During launcher-controlled retirement, the launcher retains the waitable root through cleanup and reaps it last.

The launcher closes its control endpoint to request retirement; abrupt launcher loss produces the same EOF.
The manager's single thread receives root, descendant, and control-readiness events from one `kqueue`.
Natural root exit first retires observed descendants and then closes the original process group; owner EOF with a live root closes the group and stops the root before draining observed descendants.
After clean natural-root cleanup, the manager waits for owner EOF before removing the directory and exiting.
Successful manager process exit is the primary cleanup barrier before the owner reaps the direct root.
In owned launcher mode, the launcher keeps its signals blocked until manager cleanup and direct-root reaping finish, so successful launcher exit is the server's cleanup barrier for parent loss, explicit retirement, and natural root exit.
If the launcher itself is killed or crashes, the manager still receives ownership-token EOF and performs cleanup, but the server can no longer wait synchronously for manager completion.

The manager preserves the private directory on unexpected unwind or any cleanup error because a surviving process may still use it.
It arms the adopted guard for removal only after successful cleanup proves that the directory is unused.
With no surviving owner to receive a filesystem error, directory removal itself is best effort and can leave the directory behind.

## Manager failure

Each launcher retains a blocking monitor for the manager process.
If the manager exits unsuccessfully while the launcher still retains a live, waitable root, the monitor reconstructs the root's current process tree and performs bounded process cleanup before the launcher continues.
The fallback revalidates process identities immediately before signaling and closes the still-pinned process group as a race backstop.
The fallback has no directory-cleanup state.
If the manager exits before completing its own cleanup and removal, the directory remains because a detached descendant observed only by the failed manager may still be live.
If manager exit times out, the owner requests forced exit and allows one more bounded recovery interval.
If the manager still does not exit, the owner disables fallback recovery before releasing the root's PID pin and returns an error without joining the live monitor thread.
If bounded fallback recovery has already started, the owner retains the pin until it finishes instead.
That monitor still reaps the manager if it exits later.

This fallback can recover only descendants still reachable from the root's current ancestry.
It cannot reconstruct a descendant that detached before the manager failed.
For a standalone command, successful fallback preserves the root's signal-derived exit status; a fallback error wakes the launcher with an error and leaves the directory in place.

## Standalone job control

An ordinary direct invocation gives the requested command its own process group.
When the launcher's foreground process group has no peer, it transfers foreground-terminal ownership before exec so terminal-generated signals reach the command group directly.
When a pipeline peer shares the launcher's foreground group, the launcher leaves terminal ownership unchanged.
In ordinary mode, `SIGHUP`, `SIGINT`, `SIGQUIT`, and `SIGTERM` addressed to the launcher are blocked, consumed synchronously, and relayed once to that group.
After root exit, the ordinary launcher restores its own foreground group when it transferred ownership, drains forwarded signals already pending at that boundary, restores its inherited signal mask, and closes the ownership token to request manager cleanup.
If startup or recovery cleanup stops the root, the launcher drains pending forwarded signals before restoring the mask and returning the error.
A signal received after that final drain can then follow its inherited disposition; if that terminates the launcher, the manager completes lifetime cleanup.
With `--exit-with-parent`, `SIGTERM` is reserved for managed retirement rather than relayed.
The launcher closes the ownership token, waits for manager cleanup, reaps the direct root, and only then drains pending signals and restores its inherited mask.

## Scope

One launcher-owned implementation now serves built-in and custom worker relay generations and direct `mcp-console sandbox` invocations.
The server invokes the launcher with the relay command line as its target and has no in-process sandbox construction, manager handle, startup gate, root identity, temporary-directory guard, or manager-recovery state.
The launcher retains the manager-owned process-group race backstop, with launcher fallback after manager failure, and gates the target before any configured code runs.
The direct path retains inherited standard streams, uses a dedicated target process group, and supplies the foreground-terminal and signal behavior above.
Hidden owned mode adds exact parent-exit observation and a `SIGTERM` retirement request without another control descriptor.
It does not support `Ctrl-Z` followed by `fg` or general pipeline job-control semantics.
Linux and Windows are not supported.
