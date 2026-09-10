#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.processes import capture_process_identity, child_process_identities
from support.normalization import code
from support.records import Transcript
from support.requirements import FRESH_PROCFS, LINUX_SANDBOX, PROCESS_EVENTS, requires
from support.suites import run_this_suite


@requires(LINUX_SANDBOX, FRESH_PROCFS)
def test_isolates_files_network_and_processes(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import os
        import socket
        import sys
        from pathlib import Path

        host = Path(sys.argv[1])
        assert host.read_text() == "host data"
        try:
            host.write_text("escaped")
        except OSError as error:
            assert error.errno == errno.EROFS
        else:
            raise AssertionError("host write succeeded")
        temporary = Path(os.environ["TMPDIR"])
        (temporary / "output").write_text("private data")
        assert (temporary / "output").read_text() == "private data"
        assert not Path(f"/proc/{sys.argv[2]}").exists()
        try:
            socket.create_connection(("127.0.0.1", int(sys.argv[3])))
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise AssertionError("host connection succeeded")
        print("host reads, private writes, network denial, and PID isolation verified")
        print(temporary)
        """)
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory) / "host.txt"
        host.write_text("host data")
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                script,
                str(host),
                str(os.getpid()),
                str(listener.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result
        assert result.stderr == "", result
        message, temporary = result.stdout.splitlines()
        assert host.read_text() == "host data"
        assert not Path(temporary).exists(), temporary
    return [
        {
            "command": "mcp-console sandbox -- python <isolation checks>",
            "stdout": message + "\n",
            "temporary_directory_removed": True,
        }
    ]


@requires(LINUX_SANDBOX, PROCESS_EVENTS)
def test_retires_descendants_after_exit_and_supervisor_loss(binary: Path) -> Transcript:
    # fmt: python
    script = code(r'''
        import os
        import signal
        import subprocess
        import sys
        import textwrap

        child = textwrap.dedent("""
            import signal

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            print("child ready", flush=True)
            signal.pause()
        """)

        descendant = subprocess.Popen(
            [sys.executable, "-c", child],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert descendant.stdout.readline() == "child ready\n"
        print(os.environ["TMPDIR"], flush=True)
        assert sys.stdin.readline() == "exit\n"
        raise SystemExit(23)
        ''')
    # fmt: python
    owner = code(r"""
        import os
        import signal
        import subprocess
        import sys

        subprocess.Popen(
            [
                sys.argv[1],
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                sys.executable,
                "-c",
                sys.argv[2],
            ]
        )
        signal.pause()
        """)
    transcript: Transcript = []
    for scenario in (
        "command exit",
        "owned SIGTERM",
        "launcher crash",
        "owner exit",
    ):
        process = subprocess.Popen(
            [sys.executable, "-c", owner, str(binary), script]
            if scenario == "owner exit"
            else [
                binary,
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                sys.executable,
                "-c",
                script,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        descriptors: list[int] = []
        temporary: str | None = None
        try:
            assert select.select([process.stdout], [], [], 15)[0], scenario
            temporary = process.stdout.readline().strip()
            assert temporary, (scenario, process.stderr.read())
            launcher = capture_process_identity(process.pid)
            if scenario == "owner exit":
                (launcher,) = child_process_identities(launcher)
                descriptors.append(os.pidfd_open(launcher[0]))
            roots = child_process_identities(launcher)
            assert len(roots) == 1, roots
            pending = list(roots)
            while pending:
                identity = pending.pop()
                descriptors.append(os.pidfd_open(identity[0]))
                pending.extend(child_process_identities(identity))
            if scenario == "command exit":
                process.stdin.write("exit\n")
                process.stdin.flush()
            elif scenario == "owned SIGTERM":
                process.send_signal(signal.SIGTERM)
            else:
                assert scenario in {"launcher crash", "owner exit"}
                process.kill()
            process.wait(timeout=15)
            poll = select.poll()
            for descriptor in descriptors:
                poll.register(descriptor, select.POLLIN)
            while descriptors:
                events = poll.poll(15000)
                assert events, (scenario, "descendants survived")
                for descriptor, _ in events:
                    poll.unregister(descriptor)
                    descriptors.remove(descriptor)
                    os.close(descriptor)
            # Native death links retire descendants after runner SIGKILL,
            # but only a living runner can remove private storage.
            if scenario == "launcher crash":
                assert Path(temporary).is_dir(), (scenario, temporary)
            else:
                assert not Path(temporary).exists(), (scenario, temporary)
            stderr = process.stderr.read()
            assert stderr == "", (scenario, stderr)
            expected = {
                "command exit": 23,
                "owned SIGTERM": 0,
                "launcher crash": -signal.SIGKILL,
                "owner exit": -signal.SIGKILL,
            }
            assert process.returncode == expected[scenario], (
                scenario,
                process.returncode,
            )
            transcript.append(
                {
                    "scenario": scenario,
                    "exit_code": process.returncode,
                    "stderr": stderr,
                    "descendants_exited": True,
                    "temporary_directory_removed": scenario != "launcher crash",
                }
            )
        finally:
            for descriptor in descriptors:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            if temporary:
                shutil.rmtree(Path(temporary).parent, ignore_errors=True)
    return transcript


@requires(LINUX_SANDBOX)
def test_relays_signal_and_preserves_target_status(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        import signal
        import sys


        def interrupted(number, frame):
            os.write(1, b"interrupted\n")
            sys.exit(23)


        signal.signal(signal.SIGINT, interrupted)
        # The signal may arrive before this write returns. Avoid reentering a
        # buffered stdout writer from the handler.
        os.write(1, b"ready\n")
        signal.pause()
        """)
    with subprocess.Popen(
        [binary, "sandbox", "--", sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert select.select([process.stdout], [], [], 15)[0]
        assert process.stdout.readline() == "ready\n"
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert (process.returncode, stdout, stderr) == (23, "interrupted\n", ""), (
            process.returncode,
            stdout,
            stderr,
        )
    return [{"signal": "SIGINT", "stdout": "ready\ninterrupted\n", "exit_code": 23}]


if __name__ == "__main__":
    run_this_suite(__file__)
