"""Preparation compatibility and failure peers, reached through real OpenSSH."""

import json
import struct
import sys
from pathlib import Path

mode, record, operation = sys.argv[1:]
assert operation == "ssh-prepare", operation


def read():
    header = sys.stdin.buffer.read(4)
    if not header:
        return None
    length = struct.unpack(">I", header)[0]
    return json.loads(sys.stdin.buffer.read(length))


def write(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()


def complete(id, value, confirmed=True):
    write(
        {
            "Completed": {
                "id": id,
                "result": {"Ok": value},
                "control": None,
                "confirmed": confirmed,
            }
        }
    )


opened = read()["Open"]
assert opened["version"] == 5
assert opened["mode"] == "Auto"
write(
    {
        "Hello": {
            "version": 3 if mode == "incompatible" else 5,
            "build": opened["build"],
        }
    }
)
if mode == "incompatible":
    raise SystemExit(0)
if mode == "delayed-discovery":
    with Path(record).with_suffix(".started").open("wb", buffering=0) as started:
        assert started.write(b"1") == 1
    with Path(record).with_suffix(".release").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
discovery = {
    "managed": True,
    "selections": {"r_home": "/remote-only/R", "python": None},
}
python_identity = {
    "embedding": {
        "python": "/remote-only/python",
        "libpython": "/remote-only/libpython",
        "python_home": "/remote-only",
    },
    "prefix": "/remote-only",
    "exec_prefix": "/remote-only",
    "base_prefix": "/remote-only",
    "base_exec_prefix": "/remote-only",
}
if mode in ("legacy-python", "delayed-discovery"):
    selected_python = Path(record).parent / "python"
    if not selected_python.exists():
        selected_python.symlink_to(sys.executable)
    python_identity["embedding"]["python"] = str(selected_python)
if mode.startswith("default-extension-"):
    discovery["selections"]["r_home"] = None
    discovery["native"] = {
        "selection": {
            "r_home": None,
            "python": {
                "selected": python_identity,
                "explicit": None,
                "managed": True,
                "duckdb_extension_directory": "/remote-only/extensions",
            },
        },
        "python": {
            "python": "/remote-only/python",
            "requirements": {"packages": ["numpy", "pandas", "duckdb"]},
        },
    }
complete(0, discovery)
while (message := read()) is not None:
    if message == "Close":
        if mode.startswith("default-extension-"):
            Path(record).with_suffix(".closed").touch()
            if mode == "default-extension-close-failure":
                write({"Hello": {"version": 4, "build": opened["build"]}})
                break
        write("Closed")
        break
    request = message["Run"]
    with Path(record).open("a") as stream:
        stream.write(json.dumps(request) + "\n")
    id = request["id"]
    if mode.startswith("default-extension-"):
        assert request["operation"]["DuckdbPython"]["extensions"] == ["sqlite"]
        write(
            {
                "Completed": {
                    "id": id,
                    "result": {"Err": "SQLite preparation failed"},
                    "control": None,
                    "confirmed": True,
                }
            }
        )
        continue
    if mode in ("legacy-python", "delayed-discovery"):
        operation = request["operation"]
        if operation == "Bootstrap" or "Duckdb" in operation:
            complete(id, None)
        elif "InspectPython" in operation:
            assert operation["InspectPython"] == {"executable": str(selected_python)}
            complete(id, python_identity)
        elif "R" in operation:
            library = Path(record).parent / "library"
            library.mkdir(exist_ok=True)
            complete(
                id,
                {
                    "library": str(library),
                    "r_libs": {"Unix": list(bytes(library))},
                    "requirements": operation["R"]["requirements"],
                },
            )
        else:
            python = operation["Python"]
            assert set(python) == {"requirements", "r"}, python
            complete(
                id,
                {
                    "python": str(selected_python),
                    "requirements": python["requirements"],
                },
            )
        continue
    if mode in ("truncated-result", "mismatched-chunk", "chunked-and-inline"):
        write(
            {
                "ResultChunk": {
                    "id": id + (mode == "mismatched-chunk"),
                    "text": json.dumps({"Err": "installer failed"}),
                }
            }
        )
        if mode == "chunked-and-inline":
            complete(id, None)
        break
    if mode == "unconfirmed":
        complete(id, None, confirmed=False)
        break
    if mode == "truncated":
        sys.stdout.buffer.write(struct.pack(">I", 100) + b"{}")
        sys.stdout.buffer.flush()
        break
    if mode == "missing":
        break
    if mode == "mismatched":
        complete(id + 1, None)
        break
    if request["operation"] == "Bootstrap":
        complete(id, None)
    else:
        assert mode == "malformed", mode
        complete(id, 42)
