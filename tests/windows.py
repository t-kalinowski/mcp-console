"""Native Windows acceptance tests for MCP stdio and Python packaging.

Run with `uv run --no-project tests/windows.py` after `cargo build`.
"""

import ctypes
from contextlib import ExitStack
import json
import os
from pathlib import Path
from queue import Queue
import socket
import subprocess
import sys
import tempfile
from threading import Thread
from textwrap import dedent
import unittest

from windows_relay import WindowsRelay  # noqa: F401 -- include protocol acceptance


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


class Session:
    def __init__(self, environment=None, *, relay=None):
        self.directory = tempfile.TemporaryDirectory(prefix="console windows ")
        self.errors = tempfile.TemporaryFile()
        command = [str(BINARY), "serve", "--no-sandbox"]
        if relay is not None:
            command.extend(["--worker", str(BINARY), "--relay", str(relay)])
        self.process = subprocess.Popen(
            command,
            cwd=self.directory.name,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errors,
        )
        self.timeout = 10 if environment and "TEST_RESOLVER_PID" in environment else 180
        self.messages = Queue()
        self.sequence = 0
        Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))
        self.messages.put(None)

    def request(self, method, params):
        self.sequence += 1
        message = {
            "jsonrpc": "2.0",
            "id": self.sequence,
            "method": method,
            "params": params,
        }
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        while True:
            response = self.messages.get(timeout=self.timeout)
            if response is None:
                self.errors.seek(0)
                raise AssertionError(self.errors.read().decode(errors="replace"))
            if response.get("id") == self.sequence:
                assert "error" not in response, response
                return response["result"]

    def initialize(self):
        self.request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "windows-acceptance", "version": "1"},
            },
        )
        self.process.stdin.write(
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        self.process.stdin.flush()

    def send(self, **arguments):
        return self.request("tools/call", {"name": "send", "arguments": arguments})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise
        finally:
            self.process.stdout.close()
            self.errors.close()
            self.directory.cleanup()


@unittest.skipUnless(os.name == "nt", "native Windows packaging")
class WindowsPackaging(unittest.TestCase):
    def test_concurrent_build_waits_and_recovers_after_failure(self):
        with ExitStack() as cleanup:
            root = Path(
                cleanup.enter_context(
                    tempfile.TemporaryDirectory(prefix="console packaging ")
                )
            )
            for source in ("build_backend.py", "tests/fixtures/windows_build.py"):
                (root / Path(source).name).write_bytes((ROOT / source).read_bytes())

            def start(hook):
                process = subprocess.Popen(
                    [sys.executable, str(root / "windows_build.py"), hook],
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                cleanup.callback(stop, process)
                lines = Queue()

                def read():
                    for line in process.stdout:
                        lines.put(line.strip())
                    lines.put(None)

                Thread(target=read, daemon=True).start()
                self.assertEqual(lines.get(timeout=10), "ready")
                return process, lines

            def stop(process):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

            first, first_lines = start("build_wheel")
            self.assertEqual(first_lines.get(timeout=10), "building")
            second, second_lines = start("build_editable")
            # The old CRT lock gives up after about ten seconds. A queued
            # build must remain blocked for the full duration of its owner.
            try:
                second.wait(timeout=12)
            except subprocess.TimeoutExpired:
                pass
            else:
                self.fail(f"queued build exited early: {second.stderr.read()}")
            self.assertTrue(second_lines.empty(), "concurrent Maturin builds")
            first.stdin.write("fail\n")
            first.stdin.flush()
            self.assertNotEqual(first.wait(timeout=10), 0)
            self.assertIn("fixture build failed", first.stderr.read())
            self.assertEqual(second_lines.get(timeout=10), "building")
            second.stdin.write("finish\n")
            second.stdin.flush()
            self.assertEqual(second.wait(timeout=10), 0, second.stderr.read())
            self.assertEqual(second_lines.get(timeout=10), "fixture.whl")
            archive, archive_lines = start("build_sdist")
            self.assertEqual(archive_lines.get(timeout=10), "building")
            archive.stdin.write("finish\n")
            archive.stdin.flush()
            self.assertEqual(archive.wait(timeout=10), 0, archive.stderr.read())
            self.assertEqual(archive_lines.get(timeout=10), "fixture.whl")


@unittest.skipUnless(os.name == "nt", "native Windows acceptance")
class WindowsConsole(unittest.TestCase):
    def relay_session(self, scenario):
        directory = tempfile.TemporaryDirectory(prefix="console relay ")
        self.addCleanup(directory.cleanup)
        executable = Path(directory.name) / "relay.exe"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                str(ROOT / "tests/fixtures/windows_relay.rs"),
                "-o",
                str(executable),
            ],
            check=True,
        )
        session = Session(
            dict(
                os.environ,
                TEST_RELAY_SCENARIO=scenario,
                TEST_CONSOLE_BINARY=str(BINARY),
            ),
            relay=executable,
        )
        self.addCleanup(session.close)
        session.timeout = 15
        session.initialize()
        return session

    def test_restart_after_forced_relay_retirement(self):
        session = self.relay_session("stall_shutdown")
        self.assertIn("42", json.dumps(session.send(r="42")))
        result = session.send(control="restart", r="42", timeout_ms=10000)
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        self.assertIn("42", json.dumps(session.send(r="42")))

    def test_relay_setup_error_reaches_mcp(self):
        session = self.relay_session("block_sideband")
        result = session.send(r="42")
        self.assertTrue(result.get("isError"), result)
        text = "\n".join(item.get("text", "") for item in result["content"])
        self.assertIn(
            text,
            {
                f"[failed to create worker sideband: {ctypes.FormatError(code).strip()} (os error {code})]"
                for code in (5, 231)  # Access denied or all pipe instances busy.
            },
        )

    def test_startup_eof_cancels_resolver(self):
        with tempfile.TemporaryDirectory(prefix="console startup ") as directory:
            root = Path(directory)
            ready = socket.socket()
            self.addCleanup(ready.close)
            ready.bind(("127.0.0.1", 0))
            ready.listen(1)
            ready.settimeout(8)
            source = root / "resolver.rs"
            source.write_text(r"""fn main() {
    std::fs::write(std::env::var("TEST_RESOLVER_PID").unwrap(), std::process::id().to_string()).unwrap();
    let _ready = std::net::TcpStream::connect(std::env::var("TEST_READY_ADDR").unwrap()).unwrap();
    std::thread::sleep(std::time::Duration::from_secs(60));
}
""")
            subprocess.run(
                ["rustc", str(source), "-o", str(root / "R.exe")], check=True
            )
            environment = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                TEST_RESOLVER_PID=str(root / "child.pid"),
                TEST_READY_ADDR=f"127.0.0.1:{ready.getsockname()[1]}",
            )
            environment.pop("R_HOME", None)
            process = subprocess.Popen(
                [str(BINARY), "serve", "--no-sandbox"],
                cwd=root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                connection, _ = ready.accept()
                connection.close()
                process.communicate(timeout=8)
                self.assertNotEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                if (root / "child.pid").exists():
                    subprocess.run(
                        ["taskkill", "/PID", (root / "child.pid").read_text(), "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                process.communicate(timeout=5)

    def test_resolver_descendants_are_retired(self):
        with tempfile.TemporaryDirectory(prefix="console resolver ") as directory:
            root = Path(directory)
            source = root / "resolver.rs"
            source.write_text(r"""fn main() {
    if std::env::args().any(|arg| arg == "child") {
        std::thread::sleep(std::time::Duration::from_secs(60));
    } else {
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("child").spawn().unwrap();
        std::fs::write(std::env::var("TEST_RESOLVER_PID").unwrap(), child.id().to_string()).unwrap();
        println!("ir 0.0.0");
    }
}
""")
            subprocess.run(
                ["rustc", str(source), "-o", str(root / "ir.exe")], check=True
            )
            environment = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                TEST_RESOLVER_PID=str(root / "child.pid"),
            )
            session = Session(environment)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            try:
                session.initialize()
                result = session.send(r="1L")
                self.assertIn("requires `ir`", json.dumps(result))
                pid = int((root / "child.pid").read_text())
                handle = kernel.OpenProcess(0x100000, False, pid)
                if handle:
                    try:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 1000), 0)
                    finally:
                        kernel.CloseHandle(handle)
            finally:
                pid_file = root / "child.pid"
                if pid_file.exists():
                    # Only the fixture-owned child recorded by this invocation.
                    subprocess.run(
                        ["taskkill", "/PID", pid_file.read_text(), "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                session.close()

    def session(self):
        session = Session()
        self.addCleanup(session.close)
        session.initialize()
        return session

    def test_requires_explicit_unsandboxed_mode(self):
        result = subprocess.run(
            [str(BINARY), "serve"], input=b"", capture_output=True, timeout=10
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"--no-sandbox", result.stderr)

    def test_r_persistence_and_restart(self):
        session = self.session()
        schema = session.request("tools/list", {})
        self.assertEqual([tool["name"] for tool in schema["tools"]], ["send"])
        self.assertNotIn(
            "SIGINT",
            schema["tools"][0]["inputSchema"]["properties"]["control"]["description"],
        )
        result = session.send(r="windows_value <- 40L; windows_value + 2L")
        self.assertIn("42", json.dumps(result))
        result = session.send(r="windows_value + 3L")
        self.assertIn("43", json.dumps(result))
        session.send(control="restart")
        result = session.send(r="exists('windows_value')")
        self.assertIn("FALSE", json.dumps(result))

    def test_r_startup_paths_survive_later_cells_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="console user home ") as user_home:
            session = Session(dict(os.environ, R_USER=user_home))
            self.addCleanup(session.close)
            session.initialize()
            for generation in range(2):
                if generation:
                    session.send(control="restart")
                session.send(r="invisible(gc())")
                result = session.send(
                    # fmt: r
                    r=dedent("""
                        stopifnot(
                          identical(normalizePath(R.home()), normalizePath(Sys.getenv("R_HOME"))),
                          identical(normalizePath(path.expand("~")), normalizePath(Sys.getenv("R_USER")))
                        )
                        loadNamespace("splines")
                        stopifnot(file.exists(system.file("DESCRIPTION", package = "splines")))
                        cat("startup paths intact")
                        """).strip()
                )
                self.assertFalse(result.get("isError"), result)
                self.assertIn("startup paths intact", json.dumps(result))

    def test_python_sql_and_errors(self):
        session = self.session()
        result = session.send(
            # fmt: python
            python=dedent("""
                windows_value = 40
                windows_value + 2
                """).strip()
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        result = session.send(python="windows_value + 3")
        self.assertIn("43", json.dumps(result))
        result = session.send(sql="select 42 as answer")
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        result = session.send(python="raise ValueError('windows error')")
        self.assertIn("ValueError: windows error", json.dumps(result))
        result = session.send(python="windows_value")
        self.assertIn("40", json.dumps(result))

    def test_interrupt_waiting_for_input(self):
        session = self.session()
        session.send(r="1L")
        result = session.send(r="readline('Name: ')", timeout_ms=100)
        self.assertIn("waiting for stdin", json.dumps(result))
        result = session.send(control="interrupt", timeout_ms=1000)
        self.assertNotIn("waiting for stdin", json.dumps(result))
        result = session.send(python="answer = input('Python: '); answer")
        self.assertIn("waiting for stdin", json.dumps(result))
        result = session.send(stdin="hello\n")
        self.assertIn("hello", json.dumps(result))

    def test_unicode_plots_and_raw_output(self):
        session = self.session()
        result = session.send(r='cat("caf\u00e9 \u03bb \u6f22\u5b57\\n"); plot(1:3)')
        self.assertIn(
            "caf\u00e9 \u03bb \u6f22\u5b57", json.dumps(result, ensure_ascii=False)
        )
        self.assertTrue(
            any(item["type"] == "image" for item in result["content"]), result
        )
        result = session.send(
            python='import os; os.write(1, b"raw stdout\\n"); os.write(2, b"raw stderr\\n"); "caf\u00e9 \u03bb \u6f22\u5b57"'
        )
        text = json.dumps(result, ensure_ascii=False)
        for expected in ["raw stdout", "raw stderr", "caf\u00e9 \u03bb \u6f22\u5b57"]:
            self.assertIn(expected, text)

    def test_managed_python_and_interrupt(self):
        environment = dict(os.environ)
        environment.pop("RETICULATE_PYTHON", None)
        session = Session(environment)
        self.addCleanup(session.close)
        session.initialize()
        result = session.send(
            # fmt: python
            python=dedent("""
                import packaging

                managed_value = 42
                managed_value
                """).strip()
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("42", json.dumps(result))
        result = session.send(
            # fmt: python
            python=dedent("""
                import matplotlib.pyplot as plt

                plt.plot([1, 2, 3])
                """).strip()
        )
        self.assertTrue(
            any(item["type"] == "image" for item in result["content"]), result
        )
        result = session.send(python="while True: pass", timeout_ms=100)
        self.assertIn("running", json.dumps(result))
        result = session.send(control="interrupt", timeout_ms=2000)
        self.assertNotIn("running;", json.dumps(result))
        result = session.send(python="managed_value")
        self.assertIn("42", json.dumps(result))

    def test_worker_exit_and_output_retention(self):
        session = self.session()
        result = session.send(r='cat(strrep("x", 20000))')
        self.assertLessEqual(
            sum(len(item.get("text", "").encode()) for item in result["content"]), 8192
        )
        records = Path(session.directory.name) / ".agents/console/sessions"
        self.assertTrue(records.is_dir())
        self.assertTrue(
            any(
                b"x" * 20000 in path.read_bytes()
                for path in records.rglob("*")
                if path.is_file()
            )
        )
        result = session.send(r='quit(save="no", status=7L)')
        self.assertIn("exited", json.dumps(result))
        result = session.send(r="42L")
        self.assertIn("42", json.dumps(result))

    def test_eof_retires_running_worker(self):
        session = self.session()
        result = session.send(r="cat(Sys.getpid()); repeat {}", timeout_ms=3000)
        self.assertIn("running", json.dumps(result))
        # close() has a bounded wait and fails if it must kill the server.

    def test_worker_exit_with_continuous_descendant_output(self):
        session = self.session()
        root = Path(session.directory.name)
        writer = root / "writer.py"
        writer.write_text(
            # fmt: python
            dedent("""
                import os
                import time

                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    os.write(1, b"x" * 65536)
                """)
        )
        pid_file = root / "writer.pid"
        try:
            result = session.send(
                python=f"import subprocess, sys; from pathlib import Path; child = subprocess.Popen([sys.executable, {str(writer)!r}]); Path({str(pid_file)!r}).write_text(str(child.pid))"
            )
            self.assertFalse(result.get("isError"), result)
            session.timeout = 10
            result = session.send(r='quit(save="no", status=7L)', timeout_ms=5000)
            self.assertIn("exited", json.dumps(result))
            self.assertIn("42", json.dumps(session.send(r="42L")))
        finally:
            if pid_file.exists():
                subprocess.run(
                    ["taskkill", "/PID", pid_file.read_text(), "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

    def test_sql_interrupt_preserves_catalog(self):
        session = self.session()
        result = session.send(sql="CREATE TABLE saved AS SELECT 42 AS answer")
        self.assertFalse(result.get("isError"), result)
        for initialize_python in [False, True]:
            if initialize_python:
                self.assertIn("42", json.dumps(session.send(python="42")))
            result = session.send(
                sql="SELECT SUM(SIN(i)) FROM range(1000000000) AS t(i)", timeout_ms=100
            )
            self.assertIn("running", json.dumps(result))
            result = session.send(control="interrupt", timeout_ms=3000)
            self.assertNotIn("running;", json.dumps(result))
            result = session.send(sql="SELECT * FROM saved")
            self.assertIn("42", json.dumps(result))

    def test_stdin_preserves_control_bytes(self):
        session = self.session()
        result = session.send(r="as.integer(charToRaw(readline()))", stdin="\x1a\n")
        self.assertIn("26", json.dumps(result))

    def test_input_and_interrupt(self):
        session = self.session()
        session.send(r="1L")
        result = session.send(r="answer <- readline('Name: '); answer", timeout_ms=100)
        self.assertIn("waiting for stdin", json.dumps(result))
        result = session.send(stdin="Windows\n")
        self.assertIn("Windows", json.dumps(result))
        result = session.send(r="saved <- 42; repeat {}", timeout_ms=100)
        self.assertIn("running", json.dumps(result))
        result = session.send(control="interrupt", timeout_ms=1000)
        self.assertNotIn("running;", json.dumps(result))
        result = session.send(r="saved")
        self.assertIn("42", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
