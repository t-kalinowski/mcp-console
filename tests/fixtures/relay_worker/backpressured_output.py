import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path


root = Path(os.environ["TMPDIR"])
endpoint = socket.socket(fileno=int(os.environ["MCP_CONSOLE_SIDEBAND_FD"]))
interrupted = os.open(root / "worker-interrupted", os.O_WRONLY)
shutdown = os.open(root / "worker-shutdown", os.O_WRONLY)
signal.signal(signal.SIGINT, lambda _signal, _frame: os.write(interrupted, b"1"))
(root / "worker-pid").write_text(str(os.getpid()))
endpoint.sendall(b'{"kind":"ready"}\n')
reader = endpoint.makefile("rb")
assert json.loads(reader.readline())["kind"] == "evaluate"


def produce_output() -> None:
    frame = b'{"kind":"console_output","data":"blocked output\\n"}\n'
    for _ in range(4096):
        endpoint.sendall(frame)


threading.Thread(target=produce_output, daemon=True).start()
if sys.argv[1] == "natural":
    with (root / "worker-exit").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
else:
    assert json.loads(reader.readline()) == {"kind": "shutdown"}
    assert os.write(shutdown, b"1") == 1
os._exit(0)
