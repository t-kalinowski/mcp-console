"""Windows acceptance through the server-relay and relay-worker wire protocols."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import tempfile
from threading import Thread
from textwrap import dedent
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@unittest.skipUnless(os.name == "nt", "native Windows relay")
class WindowsRelay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="console relay worker ")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.worker = Path(cls.directory.name) / "worker.exe"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                str(ROOT / "tests/fixtures/windows_worker.rs"),
                "-o",
                str(cls.worker),
            ],
            check=True,
        )

    def start(self, scenario):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        directory = tempfile.TemporaryDirectory(prefix="console relay records ")
        self.addCleanup(directory.cleanup)
        marker = Path(directory.name) / "dispatched"
        process = subprocess.Popen(
            [str(BINARY), "worker-relay", str(self.worker)],
            env=dict(
                os.environ,
                TEST_WORKER_SCENARIO=scenario,
                TEST_WORKER_READY=f"127.0.0.1:{listener.getsockname()[1]}",
                TEST_DISPATCHED=str(marker),
                TEST_CONSOLE_BINARY=str(BINARY),
                TMPDIR=directory.name,
                MCP_CONSOLE_HOME=directory.name,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop, process)
        try:
            ready = listener.accept()[0]
        except TimeoutError:
            if process.poll() is not None:
                output, errors = process.communicate()
                self.fail(
                    f"relay exited before fixture readiness ({process.returncode}): "
                    f"{output.decode(errors='replace')}{errors.decode(errors='replace')}"
                )
            raise
        ready.settimeout(10)
        stream = ready.makefile("rb")
        pid = int(stream.readline())
        if scenario.startswith("framing_"):
            self.framing_control = ready
            self.framing_stream = stream
            self.addCleanup(ready.close)
            self.addCleanup(stream.close)
        else:
            stream.close()
            ready.close()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        owned_worker = kernel.OpenProcess(0x100000, False, pid)
        self.assertTrue(owned_worker, ctypes.get_last_error())
        self.addCleanup(kernel.CloseHandle, owned_worker)
        self.assertEqual(json.loads(process.stdout.readline()), {"kind": "ready"})
        return process, kernel, owned_worker, marker

    def framing_reader(self, process):
        events = Queue()

        def read():
            for line in process.stdout:
                events.put(json.loads(line))
            events.put(None)

        reader = Thread(target=read, daemon=True)
        reader.start()
        return events, reader

    def framing_finish(self, process, events, reader):
        process.stdin.close()
        process.wait(timeout=10)
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(process.returncode, 0, process.stderr.read().decode())
        tail = []
        while (event := events.get(timeout=5)) is not None:
            tail.append(event)
        return tail

    def owned_holder(self, kernel):
        pid = int(self.framing_stream.readline())
        handle = kernel.OpenProcess(0x100001, False, pid)
        self.assertTrue(handle, ctypes.get_last_error())
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL

        def retire():
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)

        self.addCleanup(retire)
        return handle

    def test_framing_fragmented_semantics_and_retiring_tail(self):
        process, kernel, worker, _ = self.start("framing_fragmented")
        events, reader = self.framing_reader(process)
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        self.assertEqual(self.framing_stream.readline(), b"prefix consumed\n")
        self.assertTrue(events.empty(), "incomplete semantic frame was forwarded")
        self.framing_control.sendall(b"1")
        self.assertEqual(
            events.get(timeout=10), {"kind": "console_output", "data": "fragmented 🦀"}
        )
        self.assertEqual(events.get(timeout=10), {"kind": "completed"})
        holder = self.owned_holder(kernel)
        self.framing_control.sendall(b"1")
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        # Keep stdin and the inherited writer live until retirement completes.
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(kernel.WaitForSingleObject(holder, 0), 258)
        tail = self.framing_finish(process, events, reader)
        self.assertEqual(
            tail,
            [
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_framing_fragmented_commands_and_batch_tail(self):
        import msvcrt

        process, kernel, _, marker = self.start("framing_echo")
        events, reader = self.framing_reader(process)
        source = "command 🦀" + "x" * (128 * 1024)
        frame = json.dumps(
            {"kind": "evaluate", "language": "r", "source": source}, ensure_ascii=False
        ).encode()
        split = frame.index("🦀".encode()) + 1
        process.stdin.write(frame[:split])
        process.stdin.flush()
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.FlushFileBuffers.restype = wintypes.BOOL
        consumed = Queue()

        def flush():
            consumed.put(
                kernel.FlushFileBuffers(msvcrt.get_osfhandle(process.stdin.fileno()))
            )

        barrier = Thread(target=flush, daemon=True)
        barrier.start()
        self.assertTrue(consumed.get(timeout=10), ctypes.get_last_error())
        barrier.join(timeout=5)
        self.assertFalse(marker.exists(), "partial command was dispatched")
        process.stdin.write(frame[split:] + b'\r\n{"kind":')
        process.stdin.flush()
        self.assertEqual(events.get(timeout=10), {"kind": "completed"})
        self.assertEqual(json.loads(marker.read_bytes())["source"], source)
        tail = self.framing_finish(process, events, reader)
        self.assertIn(
            {
                "kind": "fatal",
                "message": "relay stdin frame is invalid: relay stdin closed midway through a frame",
            },
            tail,
        )

    def test_framing_inherited_writer_cannot_extend_retirement(self):
        process, kernel, worker, _ = self.start("framing_refill")
        events, reader = self.framing_reader(process)
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        holder = self.owned_holder(kernel)
        self.framing_control.sendall(b"1")
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(kernel.WaitForSingleObject(holder, 0), 258)
        tail = self.framing_finish(process, events, reader)
        outputs = [event for event in tail if event["kind"] == "console_output"]
        self.assertTrue(outputs)
        self.assertTrue(
            all(
                event == {"kind": "console_output", "data": "inherited writer"}
                for event in outputs
            )
        )
        self.assertEqual(
            tail,
            outputs
            + [
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_framing_malformed_empty_and_partial_eof_diagnostics(self):
        for scenario, diagnostic in (
            ("framing_malformed", "EOF while parsing a value at line 2 column 0"),
            ("framing_empty", "EOF while parsing a value at line 2 column 0"),
            ("framing_partial", "worker sideband closed midway through a frame"),
        ):
            with self.subTest(scenario=scenario):
                process, _, _, _ = self.start(scenario)
                process.stdin.write(
                    b'{"kind":"evaluate","language":"r","source":"42"}\n'
                )
                process.stdin.flush()
                process.wait(timeout=10)
                events = self.finish(process)
                self.assertIn(
                    {
                        "kind": "fatal",
                        "message": f"worker sideband read failed: {diagnostic}",
                    },
                    events,
                )

    def test_framing_builtin_retains_partial_command_across_interrupt(self):
        process, _, _, _ = self.start("framing_interrupt")
        events, reader = self.framing_reader(process)
        process.stdin.write(
            b'{"kind":"evaluate","language":"r","source":"retained <- 0L"}\n'
        )
        process.stdin.flush()
        self.assertEqual(events.get(timeout=15), {"kind": "completed"})
        command = {
            "kind": "evaluate",
            "language": "r",
            "source": 'retained <- retained + 1L; cat("🦀", retained)',
        }
        process.stdin.write(json.dumps(command, ensure_ascii=False).encode() + b"\r\n")
        process.stdin.flush()
        self.assertEqual(self.framing_stream.readline(), b"prefix consumed\n")
        # This output proves the real worker returned from its incomplete read
        # and handled the idle interrupt before we release the UTF-8 suffix.
        self.assertEqual(
            events.get(timeout=15), {"kind": "console_output", "data": "\n"}
        )
        self.framing_control.sendall(b"1")
        output = []
        while (event := events.get(timeout=15))["kind"] != "completed":
            self.assertEqual(event["kind"], "console_output", event)
            output.append(event["data"])
        self.assertEqual("".join(output), "🦀 1")
        process.stdin.write(b'{"kind":"shutdown","grace_millis":1000}\n')
        process.stdin.flush()
        tail = self.framing_finish(process, events, reader)
        self.assertNotIn("fatal", [event["kind"] for event in tail])

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()

    def finish(self, process):
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
        return [json.loads(line) for line in output.splitlines()]

    def test_rejects_unterminated_commands(self):
        for command in (
            {"kind": "evaluate", "language": "r", "source": "42"},
            {"kind": "stdin", "data": "x"},
        ):
            with self.subTest(command=command):
                process, _, _, marker = self.start("commands")
                process.stdin.write(json.dumps(command).encode())
                process.stdin.flush()
                events = self.finish(process)
                self.assertTrue(
                    any(
                        event["kind"] == "fatal"
                        and "midway through a frame" in event["message"]
                        for event in events
                    ),
                    events,
                )
                self.assertFalse(marker.exists(), "unterminated command was dispatched")

    def test_fatal_precedes_sideband_closure(self):
        process, _, _, _ = self.start("invalid_sideband")
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        # Keep the input open: the malformed sideband must initiate retirement.
        process.wait(timeout=10)
        events = self.finish(process)
        kinds = [event["kind"] for event in events]
        fatal = next(event for event in events if event["kind"] == "fatal")
        self.assertIn(
            "worker sideband read failed: unknown variant `broken`", fatal["message"]
        )
        self.assertLess(kinds.index("fatal"), kinds.index("worker_sideband_closed"))

    def test_stdin_write_failure_retires_worker(self):
        process, _, _, _ = self.start("closed_stdin")
        process.stdin.write(b'{"kind":"stdin","data":"hello"}\n')
        process.stdin.flush()
        process.wait(timeout=10)
        events = self.finish(process)
        self.assertTrue(
            any(
                event["kind"] == "fatal"
                and "worker stdin write failed:" in event["message"]
                for event in events
            ),
            events,
        )

    def test_drains_sideband_after_exit(self):
        process, kernel, worker, _ = self.start("exit_tail")
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        # Fill the relay's bounded event queue before allowing stdout to drain.
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        events = self.finish(process)
        self.assertEqual(
            [event["data"] for event in events if event["kind"] == "console_output"],
            [f"{index:04}" for index in range(1024)],
        )
        self.assertEqual(
            events[-6:],
            [
                {"kind": "image", "data": "eA==", "mime_type": "image/png"},
                {"kind": "completed"},
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_r_home_environment_precedence(self):
        # Exercise R startup directly, independently of third-party resolver
        # home-directory requirements (notably DuckDB's USERPROFILE lookup).
        with tempfile.TemporaryDirectory(prefix="console HOME ") as home:
            for r_user, profile in (
                (None, os.environ.get("USERPROFILE")),
                (None, None),
                ("", None),
            ):
                with self.subTest(r_user=r_user, profile=profile):
                    environment = dict(os.environ, HOME=home, TMPDIR=home)
                    for key, value in (("R_USER", r_user), ("USERPROFILE", profile)):
                        if value is None:
                            environment.pop(key, None)
                        else:
                            environment[key] = value
                    process = subprocess.Popen(
                        [str(BINARY), "worker-relay", str(BINARY), "worker"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.addCleanup(self.stop, process)
                    events = Queue()

                    def read():
                        for line in process.stdout:
                            events.put(json.loads(line))
                        events.put(None)

                    reader = Thread(target=read, daemon=True)
                    reader.start()
                    try:
                        self.assertEqual(events.get(timeout=15), {"kind": "ready"})
                        # fmt: r
                        source = dedent("""
                            stopifnot(identical(
                              normalizePath(path.expand("~")),
                              normalizePath(Sys.getenv("HOME"))
                            ))
                            cat("HOME selected")
                            """).strip()
                        command = {
                            "kind": "evaluate",
                            "language": "r",
                            "source": source,
                        }
                        process.stdin.write(json.dumps(command).encode() + b"\n")
                        process.stdin.flush()
                        output = []
                        while True:
                            event = events.get(timeout=15)
                            self.assertIsNotNone(event, output)
                            if event["kind"] == "completed":
                                break
                            output.append(event)
                        self.assertIn("HOME selected", json.dumps(output))
                    finally:
                        process.stdin.close()
                        process.wait(timeout=10)
                        reader.join(timeout=5)

    def test_retains_failure_discovered_during_exit_drain(self):
        process, kernel, worker, _ = self.start("exit_invalid")
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        events = self.finish(process)
        self.assertEqual(
            [event["data"] for event in events if event["kind"] == "console_output"],
            [f"{index:04}" for index in range(1024)],
        )
        self.assertEqual(
            [event["kind"] for event in events[-5:]],
            [
                "stdout_closed",
                "stderr_closed",
                "fatal",
                "worker_sideband_closed",
                "worker_exited",
            ],
        )
        self.assertIn("unknown variant `broken`", events[-3]["message"])


if __name__ == "__main__":
    unittest.main()
