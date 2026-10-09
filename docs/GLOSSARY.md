# Glossary

## User terms

**Session:** The persistent workspace owned by one MCP connection.
It can outlive several workers; there is no named-session API.

**Cell:** One complete R, Python, or SQL submission.
Source and interactive input are separate.

**Poll:** A `send` call without code that waits for new output or completion.
It does not rerun the prior cell.

**Requirements / declaration / manifest:** The accepted dependency request, not an installed-package inventory or lockfile.
It survives worker restart, not a new server connection.

**Preparation:** Making dependencies available.
It does not import, attach, or load them for the user.

**Recording:** Files containing calls, source, input, output, and artifacts.
They are unredacted records, not live-state checkpoints.

## Developer terms

**Server / controller:** The process speaking MCP.
It owns admission, retained declarations, bounded responses, and recording.

**Worker:** The process holding interpreter and database state.
The built-in worker coordinates R, Python, and SQL on one execution thread.

**Generation:** One worker lifetime and all work tied to it.
Old input, callbacks, evaluations, and candidate commits must not reach its replacement.

**Relay:** The process translating worker sideband and standard streams, delivering interruption, and reaping the direct worker.
It is not the native sandbox supervisor.

**Sideband:** Private JSONL commands and semantic events, separate from stdin/stdout/stderr.

**Native runner:** The verified private executable owning native policy enforcement, temporary storage, and its descendant-cleanup contract.

**Preparation owner:** The machinery owning discovery/materializer processes and their cleanup.
The server, not this owner, accepts declarations.

**Candidate / activation:** A proposed environment and the step making it usable by a worker.
Materialization, activation, and server acceptance are distinct.

**Retirement / cleanup receipt:** Stopping an owned resource and obtaining confirmation that its owner's cleanup contract completed.
Stream closure alone is not that confirmation.

**Output cut:** A finite boundary selected for one response.
It does not establish chronology across independent streams.

**Execution host:** The machine or environment where the client, Console, and its files/processes run.

See [Architecture](ARCHITECTURE.md) for relationships between these components.
