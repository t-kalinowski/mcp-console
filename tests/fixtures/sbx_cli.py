"""Deterministic local provider peer: public create/exec/rm/ls, no VM claims."""

import json
import os
import select
import signal
import struct
import subprocess
import sys
import uuid
from pathlib import Path

root = Path(os.environ["CONSOLE_SBX_PEER"])
args = sys.argv[1:]
with (root / "calls").open("a") as stream:
    stream.write(
        json.dumps({"args": args, "pid": os.getpid(), "ppid": os.getppid()}) + "\n"
    )
mode = (root / "mode").read_text() if (root / "mode").exists() else ""
state = root / "vms"


def gate() -> None:
    with (root / "reached").open("wb", buffering=0) as stream:
        stream.write(b"1")
    signal.pause()


real = os.environ.get("CONSOLE_SBX_REAL")
if mode == "diagnostics" and args[0] in ("version", "ls", "create", "rm"):
    print(f"fixture: {args[0]} diagnostic", file=sys.stderr)
    if args[0] in ("create", "rm"):
        print(f"fixture: {args[0]} progress")
if real:
    if args[0] == "ls" and mode == "cleanup-loss":
        sys.exit("fixture: daemon unavailable")
    if args[0] == "ls" and mode == "cleanup-hang":
        signal.pause()
    if args[0] == "create" and mode == "create-unacknowledged":
        gate()
    if args[0] == "create" and mode == "create-gate":
        subprocess.run([real, *args], check=True)
        gate()
    if args[0] == "exec" and (
        (args[-1] == "docker-sandbox-probe" and mode == "probe-gate")
        or (args[-1] == "docker-sandbox-launch" and mode == "launch-gate")
    ):
        gate()
    os.execv(real, [real, *args])


# A rejected frame can close the attachment before this peer's next write.
# Match ordinary CLI pipe termination without adding a Python traceback.
signal.signal(signal.SIGPIPE, signal.SIG_DFL)


if args == ["version"]:
    if mode == "diagnostics-gate":
        print("provider startup\n" * 20000, end="", file=sys.stderr, flush=True)
        gate()
    print(
        "sbx version: v0.42.0 fixture"
        if mode == "unsupported-version"
        else (root / "version").read_text()
        if (root / "version").exists()
        else "sbx version: v0.42.1 fixture"
    )
elif args[0] == "ls":
    if mode == "malformed-listing":
        print('{"sandboxes": [{"name": "missing-id"}]}')
        sys.exit(0)
    if mode == "cleanup-hang" and state.exists():
        gate()
    if mode == "cleanup-loss" and state.exists():
        sys.exit("fixture: daemon unavailable")
    print(
        json.dumps(
            {"sandboxes": json.loads(state.read_text()) if state.exists() else []}
        )
    )
elif args[0] == "create":
    name = args[args.index("--name") + 1]
    if mode == "create-unacknowledged":
        gate()
    if mode == "create-failed":
        sys.exit("fixture: create rejected")
    mounts = args[args.index("shell") + 1 :]
    current = json.loads(state.read_text()) if state.exists() else []
    current.append(
        {
            "name": name,
            "id": str(uuid.uuid4()),
            "agent": "shell",
            "status": "running",
            **({"workspaces": mounts} if mounts else {}),
        }
    )
    state.write_text(json.dumps(current))
    if mode == "signal-create":
        print("fixture: original creation diagnostic", file=sys.stderr, flush=True)
    if mode in ("create-gate", "signal-create"):
        gate()
elif args[0] == "rm":
    assert args[1] == "--force"
    if mode == "removal-failed":
        sys.exit("fixture: removal failed")
    remaining = [vm for vm in json.loads(state.read_text()) if vm["name"] != args[2]]
    if remaining:
        state.write_text(json.dumps(remaining))
    else:
        state.unlink()
elif args[0] == "exec":
    assert args[1:3] == ["-i", "--workdir"] and "-t" not in args
    source = sys.stdin.buffer
    length = struct.unpack(">I", source.read(4))[0]
    bootstrap = json.loads(source.read(length))
    assert args[3] == bootstrap["workspace"]
    assert bootstrap["provider"] == "compute"
    assert set(bootstrap["policy"]) <= {"environment", "inherit_environment"}
    probe = args[-1] == "docker-sandbox-probe"
    frame_log = root / "probe-frames"
    capture_frames = probe and frame_log.exists()

    def frame(tag: int, value: dict) -> None:
        payload = json.dumps(value).encode() + (b"\n" if tag == 2 else b"")
        data = struct.pack(">BI", tag, len(payload)) + payload

        def receipt(stage: str, **evidence: int) -> None:
            # Opt-in peer evidence stays off protocol stdout. A completed peer
            # write does not establish an owner read or forwarded-frame receipt.
            # "written" is buffered acceptance; "flushed" records the peer flush.
            if capture_frames:
                with frame_log.open("a") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "tag": tag,
                                "length": len(payload),
                                "stage": stage,
                                **evidence,
                            }
                        )
                        + "\n"
                    )

        receipt("started")
        written = sys.stdout.buffer.write(data)
        receipt("written", bytes=written)
        sys.stdout.buffer.flush()
        receipt("flushed")

    if (probe and mode == "probe-gate") or (not probe and mode == "launch-gate"):
        gate()
    assert "version" not in bootstrap, bootstrap
    hello = {"build": "unsupported" if mode == "probe-build" else bootstrap["build"]}
    if mode == "probe-unknown":
        hello["unexpected"] = True
    if mode == "missing-build":
        hello.pop("build")
    frame(1, hello)
    if probe and mode == "probe-closed-output":
        if os.environ.get("MCP_CONSOLE_TEST_PREPARED_PROBE"):
            # Keep the original HELLO and SIGPIPE failure. The public test
            # releases this peer only after the owner accepted its own header.
            with (root / "peer-ready").open("wb", buffering=0) as reached:
                reached.write(b"1")
            release = os.open(root / "peer-release", os.O_RDONLY)
            abort = os.open(root / "abort", os.O_RDONLY)
            ready, _, _ = select.select([release, abort], [], [])
            if abort in ready:
                sys.exit(0)
            assert os.read(release, 1) == b"1"
            os.close(release)
            os.close(abort)
        # Keep the attachment pipe open until this peer exits, so the owner
        # cannot cancel the peer before its next write hits the closed reader.
        attachment = os.dup(1)
        reader, writer = os.pipe()
        os.close(reader)
        os.dup2(writer, 1)
        os.close(writer)
    if probe:
        native_only = mode.startswith("native-")
        home = None if native_only else "/usr/lib/R"
        prefix = "/target-only" if native_only else "/opt/analysis"
        executable = prefix + ("/bin/python3" if native_only else "/bin/python")
        runtime = {
            "discovery": {
                "managed": False,
                "selections": {"r_home": home, "python": None},
            },
            "r": None,
            "python": None,
            "native": {
                "r_home": home,
                "python": {
                    "selected": {
                        "embedding": {
                            "python": executable,
                            "libpython": prefix + "/lib/libpython.so",
                            "python_home": prefix,
                        },
                        "prefix": prefix,
                        "exec_prefix": prefix,
                        "base_prefix": prefix,
                        "base_exec_prefix": prefix,
                        "metadata": {
                            "base_executable": executable,
                            "pythonpath": prefix,
                            "version": "3.14.0",
                            "version_number": "3.14",
                            "architecture": "64bit",
                            "conda": False,
                            "numpy": None,
                        },
                    },
                    "explicit": None,
                    "managed": False,
                    "duckdb_extension_directory": None,
                },
            },
        }
        if mode == "r-only-probe":
            runtime["native"]["python"] = None
        if mode == "missing-python-metadata":
            runtime["native"]["python"]["selected"].pop("metadata")
        if mode == "native-managed":
            runtime["native"]["python"]["managed"] = True
        if mode == "native-r-conflict":
            runtime["discovery"]["selections"]["r_home"] = "/usr/lib/R"
        if mode == "native-relative":
            runtime["native"]["python"]["selected"]["embedding"]["python"] = (
                "relative/python"
            )
        if mode == "native-prefix":
            runtime["native"]["python"]["selected"]["embedding"]["python_home"] = (
                "/other"
            )
        if mode == "native-unknown":
            runtime["native"]["unused"] = "unsupported"
        if mode == "native-embedding-unknown":
            runtime["native"]["python"]["selected"]["embedding"]["unused"] = (
                "unsupported"
            )
        if mode == "probe-managed":
            runtime["discovery"]["managed"] = True
        if mode == "probe-oversized":
            runtime["discovery"]["selections"]["r_home"] = "/" + "r" * (64 * 1024)
        if mode != "missing-runtime":
            frame(2 if mode == "probe-data" else 4, runtime)
        if mode == "duplicate-runtime":
            frame(4, runtime)
        if mode == "probe-extra":
            print("unframed startup output", flush=True)
        frame(
            3,
            {
                "confirmed": mode != "probe-unconfirmed",
                "error": "probe validation failed" if mode == "probe-failed" else None,
            },
        )
    else:
        frame(2, {"kind": "ready"})
        initializing = mode == "bootstrap-input"
        if initializing:
            frame(2, {"kind": "input_requested", "prompt": "target startup> "})
        else:
            frame(2, {"kind": "runtime_initialized", "interrupted": False})
        for line in source:
            command = json.loads(line)
            if command["kind"] == "stdin":
                assert initializing and command["data"] == "continue\n", command
                initializing = False
                frame(2, {"kind": "input_received"})
                frame(2, {"kind": "runtime_initialized", "interrupted": False})
            elif command["kind"] == "evaluate":
                assert not initializing, "cell reached worker before runtime bootstrap"
                with (root / "evaluations").open("a") as stream:
                    stream.write(json.dumps(command) + "\n")
                if mode == "signal-output":
                    # Pressure belongs to the admitted cell, after evaluate receipt.
                    with (root / "reached").open("wb", buffering=0) as stream:
                        stream.write(b"1")
                    with (root / "release").open("rb", buffering=0) as stream:
                        assert stream.read(1) == b"1"
                    print(
                        "fixture: original attachment diagnostic",
                        file=sys.stderr,
                        flush=True,
                    )
                    for _ in range(2048):
                        frame(2, {"kind": "console_output", "data": "x" * 32768})
                frame(2, {"kind": "console_output", "data": "provider peer\n"})
                frame(2, {"kind": "completed"})
            elif command["kind"] == "shutdown":
                frame(2, {"kind": "shutdown_started"})
                frame(2, {"kind": "worker_exited", "code": 0})
                frame(3, {"confirmed": True, "error": None})
                break
else:
    raise AssertionError(args)
