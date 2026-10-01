# Glossary

**Session** — The logical console owned by one MCP server connection. There is
one implicit session, not a named-session API. It can outlive several workers.

**Cell** — A complete R, Python, or SQL submission. Cells execute one at a time;
interactive input is separate from source code.

**Operation** — Server-owned work admitted by a `send` call, such as evaluation,
preparation, or control. A call may stop waiting before its admitted work ends.

**Worker generation** — One worker lifetime and the state tied to it. Restart
creates a new generation. Old callbacks, stdin, and evaluations must not reach
the replacement.

**Server / controller** — The process speaking MCP to the client. It owns
admission, requirements, bounded responses, and recording, even when execution
is remote.

**Relay** — The process between server and worker. It translates the worker's
sideband and standard streams into relay events, delivers signals, and reaps
its direct worker. It is not the sandbox supervisor.

**Worker** — The process holding live interpreter and database state. The
built-in worker coordinates R, Python, and SQL on one execution thread; a
custom worker implements the [worker protocol](WORKER_PROTOCOL.md).

**Sideband** — Private framed messages between relay and worker, separate from
stdin, stdout, and stderr. It carries commands, semantic output, and completion.

**Target / execution host** — Where code runs: the local host, an SSH host, a
Docker container, or a Docker Sandbox microVM. Target paths belong there, not
necessarily on the controller.

**Native runner** — The verified private sandbox executable. It owns native
policy enforcement, temporary storage, and descendant supervision. Console
integrates it as an ordinary child process.

**Compute provider** — An outer execution boundary such as Docker or Docker
Sandbox. Disabling the inner native sandbox does not remove that boundary.

**Preparation owner / resolver** — Trusted execution-host machinery that
selects interpreters and prepares dependencies outside the worker sandbox.
The server owns the requested manifest and decides whether to accept results.

**Manifest / retained requirements** — The server's accepted dependency
declaration. It survives worker restart; live variables and database state do
not. A resolved environment is a concrete result of preparing that declaration.

**Candidate / activation** — A proposed environment and the transition that
makes it usable by the worker. Resolving a candidate is not the same as
publishing it or committing the retained manifest.

**Retirement / cleanup receipt** — Stopping an owned resource and obtaining the
owner's confirmation that its cleanup contract completed. Transport exit alone
is not proof of remote or compute-resource retirement.

**Output cut** — A finite boundary in the server's ordered output tape used to
construct one response. It is not a global timestamp order across streams.

**Recording** — Controller-side files containing calls, source, input, output,
and artifacts. Recordings are unredacted and are not live-state checkpoints.
