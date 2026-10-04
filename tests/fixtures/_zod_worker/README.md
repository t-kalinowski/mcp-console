# Zod worker fixture

The adjacent executable `../zod` launches this private package with the harness-selected `MCP_CONSOLE_TEST_PYTHON`.
Keep the launcher and package together when staging the fixture.
Python loads the sibling package from the launcher's directory, so callers can use either shell or explicit Python invocation from another working directory.

`core.py` owns the worker loop, scenario dispatch, and local state.
`protocol.py` owns the separate sideband read/write pipes and JSONL messages.
`control.py` owns fixture FIFOs, descriptor globals, markers, and causal gates.
`io.py` owns raw stdin/output operations, and `startup.py` composes startup scenarios.

Importing the package defines helpers without opening descriptors, consuming environment variables, installing signal handlers, or starting the worker.
The launcher calls `main()` to perform setup.
Keep child-worker implementation independent of the parent test harness and preserve existing payloads, markers, descriptor lifetimes, and gates when moving scenarios.
