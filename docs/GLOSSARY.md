# Glossary

<span id="user-terms"></span>

**Agent:** The model-driven program that submits code and uses its results.

**Client:** The application that connects to Console, such as a coding assistant or a program using a Console adapter.

**MCP:** Model Context Protocol, the interface through which the client discovers and calls Console's tool.

**Console process:** One lifetime of the Console server, from startup to shutdown.
A process can contain multiple successive sessions.

**Session:** One lifetime of a worker and its live R, Python, and SQL state.
Objects remain available between calls.
Replacing a worker clears its live state.
Each newly launched worker starts another session within the same Console process; a restart request alone does not establish one.

**Cell:** One complete block of R, Python, or SQL code submitted for execution.
Interactive input is supplied separately.

**REPL:** Read–evaluate–print loop: run code, inspect the result, and continue in the same session.

**Execution host:** The machine or environment where Console runs and stores its files.
Console and the client's shell and filesystem tools should use the same host and project.

**Managed environment:** An environment whose packages Console prepares for the session.

**Existing environment:** A Python installation or virtual environment selected by the user.
Console uses its installed packages rather than preparing them.

**Preparation:** Making dependencies available.
Code still needs to import a Python module, attach an R package, or load an extension when appropriate.

**Requirements:** The requested dependencies.
They are not an inventory of installed packages or a lockfile.
Accepted requirements survive session restarts within the same Console process.

**Recording:** Saved material belonging to one Console process.
Recordings are unredacted records, not live-state checkpoints.
A recording can include multiple sessions.

**Capture:** The authoritative retained events, text, and images.

**Transcript:** The human-readable account of interactions across a recording.

**Sandbox:** Operating-system restrictions on the code Console runs.
Default permissions restrict writes and direct networking but allow host-file reads, including readable secrets.

For polling and other client operations, see [Calls, input, and control](SEND_OPERATIONS.md).
Internal terms such as worker, relay, generation, and retirement are defined in [Architecture](ARCHITECTURE.md).

<span id="developer-terms"></span>

Contributor vocabulary is in [Architecture](ARCHITECTURE.md#developer-terms).
