#!/usr/bin/env -S uv run --script

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.checkpoints import FifoCheckpoint
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, WORKER, requires
from support.ssh import bootstrap, read_frame
from support.suites import run_this_suite


def _connection_closed(
    binary: Path, before_ready: bool, sandbox: bool = True
) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        checkpoint = FifoCheckpoint.create(root / "reached")
        state = root / "worker-state"
        setup = (
            code("""
                writeLines(c(as.character(Sys.getpid()), Sys.getenv('TMPDIR')), STATE)
                con <- fifo(CHECKPOINT, open='wb', blocking=TRUE)
                writeBin(as.raw(49), con); close(con)
                """)
            .replace("STATE", json.dumps(str(state)))
            .replace("CHECKPOINT", json.dumps(str(checkpoint.path)))
        )
        policy = {
            "extends": ":workspace",
            "environment": {
                "R_HOME": environment["R_HOME"],
            },
        }
        if before_ready:
            # Gate the installed-R discovery command before the worker can send
            # Ready. R itself starts with --vanilla and does not load profiles.
            gate = root / "hold"
            os.mkfifo(gate)
            r = root / "R"
            r.write_text(
                code(r"""
                    #!/bin/sh
                    printf '%s\n' "$PPID" "$TMPDIR" > STATE
                    printf 1 > CHECKPOINT
                    exec /bin/cat GATE
                    """)
                .replace("STATE", shlex.quote(str(state)))
                .replace("CHECKPOINT", shlex.quote(str(checkpoint.path)))
                .replace("GATE", shlex.quote(str(gate)))
            )
            r.chmod(0o755)
            policy["inherit_environment"] = False
            policy["environment"] = {"PATH": str(root)}
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        process = subprocess.Popen(
            [binary, "ssh-launch"],
            stdin=subprocess.PIPE,
            stdout=write_fd,
            stderr=subprocess.PIPE,
        )
        assert process.stdin is not None
        reader = os.fdopen(read_fd, "rb", buffering=0)
        try:
            process.stdin.write(
                bootstrap(binary, root, policy=policy, no_sandbox=not sandbox)
            )
            process.stdin.flush()
            hello = read_frame(reader)
            assert hello[0] == 1, hello
            if not before_ready:
                # Fill the output pipe to EAGAIN ourselves, before admitting the
                # cell. No sleeps or assumptions about kernel buffer capacity.
                while True:
                    try:
                        os.write(write_fd, b"x" * 65536)
                    except BlockingIOError:
                        break
                process.stdin.write(
                    json.dumps(
                        {
                            "kind": "evaluate",
                            "language": "r",
                            "source": setup + "repeat cat(strrep('x', 65536))",
                        }
                    ).encode()
                    + b"\n"
                )
                process.stdin.flush()
            checkpoint.wait("remote worker entered the gated operation")
            pid, private = state.read_text().splitlines()
            if sandbox:
                assert Path(private).is_dir()
            process.stdin.close()
            process.wait(timeout=10)
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(
                    "remote worker survived observed connection closure"
                )
            if sandbox:
                assert not Path(private).exists(), private
            assert "SSH connection closed" in process.stderr.read().decode()
            return [
                {
                    "before_readiness": before_ready,
                    "worker_retired": True,
                    "private_storage_removed": sandbox,
                    "local_deadline_seconds": 10,
                }
            ]
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            os.close(write_fd)
            reader.close()
            checkpoint.close()


@requires(WORKER, SANDBOX)
def test_connection_closure_before_readiness(binary: Path) -> Transcript:
    return _connection_closed(binary, before_ready=True)


@requires(WORKER, SANDBOX)
def test_connection_closure_with_backpressured_output(binary: Path) -> Transcript:
    return _connection_closed(binary, before_ready=False)


@requires(WORKER)
def test_direct_connection_closure_with_backpressured_output(
    binary: Path,
) -> Transcript:
    return _connection_closed(binary, before_ready=False, sandbox=False)


if __name__ == "__main__":
    run_this_suite(__file__)
