"""Unix worker peer for the portable relay lifecycle scenarios."""

import json
import os
import signal
import sys
from pathlib import Path

scenario = sys.argv[1]
reader = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD"))
writer = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD"))
if scenario == "closed_stdin":
    os.close(0)
Path(os.environ["TEST_WORKER_PID"]).write_text(str(os.getpid()))
with os.fdopen(reader, "rb") as source, os.fdopen(writer, "wb", buffering=0) as sink:
    sink.write(b'{"kind":"ready"}\n')
    if scenario == "setup_failure":
        os.write(1, b"setup stdout\n")
        os.write(2, b"setup stderr\n")
        with open(os.environ["TEST_WORKER_READY"], "wb", buffering=0) as ready:
            ready.write(b"1")
        signal.pause()
    elif scenario == "closed_stdin":
        # The relay must retire us when the independent stdin writer fails.
        source.read()
    else:
        assert json.loads(source.readline())["kind"] == "evaluate"
        if scenario == "invalid_sideband":
            sink.write(b'{"kind":"broken"}\n')
            source.read()
        else:
            assert scenario in {"exit_tail", "exit_invalid"}, scenario
            frames = [
                {"kind": "console_output", "data": f"{index:04}"}
                for index in range(1024)
            ]
            frames.extend(
                [{"kind": "broken"}]
                if scenario == "exit_invalid"
                else [
                    {"kind": "image", "data": "eA==", "mime_type": "image/png"},
                    {"kind": "completed"},
                ]
            )
            sink.write(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
