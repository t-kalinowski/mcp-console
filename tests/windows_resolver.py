"""Windows acceptance for the same hidden resolver protocol used on Unix."""

import json
import ctypes
import os
from pathlib import Path
from queue import Queue
import shutil
import socket
import subprocess
import sys
import tempfile
from threading import Thread
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


class Resolver:
    def __init__(self, directory, environment, mode="PythonOnly"):
        self.process = subprocess.Popen(
            [BINARY, "resolve"],
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.messages = Queue()

        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put(None)

        Thread(target=read, daemon=True).start()
        build = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
        self.send(
            {
                "Open": {
                    "version": 6,
                    "build": build,
                    "workspace": "",
                    "selections": {},
                    "mode": mode,
                }
            }
        )

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout=30):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise AssertionError(self.process.stderr.read().decode(errors="replace"))
        return message

    def ready(self):
        assert "Hello" in self.receive()
        discovery = self.receive()["Completed"]
        assert discovery["confirmed"], discovery
        assert "Ok" in discovery["result"], discovery
        return discovery["result"]["Ok"]

    def run(self, id, operation, timeout=30):
        self.send({"Run": {"id": id, "operation": operation}})
        completed = self.receive(timeout)["Completed"]
        assert completed["id"] == id, completed
        assert completed["confirmed"], completed
        return completed

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


@unittest.skipUnless(os.name == "nt", "native Windows resolver")
class WindowsResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory(
            prefix="console resolver fixture "
        )
        cls.fixture = Path(cls.fixture_directory.name) / "resolver.exe"
        subprocess.run(
            [
                "rustc",
                str(ROOT / "tests/fixtures/windows_resolver.rs"),
                "-o",
                str(cls.fixture),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="console resolver 日本語 ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("uv.exe", "ir.exe"):
            shutil.copyfile(self.fixture, self.root / name)
        self.library = self.root / "R library 日本語"
        self.library.mkdir()
        self.r_home = self.root / "R home"
        (self.r_home / "bin").mkdir(parents=True)
        shutil.copyfile(self.fixture, self.r_home / "bin/Rscript.exe")
        self.record = self.root / "commands.jsonl"
        self.environment = dict(
            os.environ,
            PATH=str(self.root),
            R_HOME=str(self.r_home),
            MCP_CONSOLE_HOME=str(self.root / "home"),
            TEST_RESOLVER_RECORD=str(self.record),
            TEST_RESOLVER_PYTHON=sys.executable,
            TEST_RESOLVER_LIBRARY=str(self.library),
        )
        self.environment.pop("RETICULATE_PYTHON", None)
        self.environment.pop("RETICULATE_UV", None)

    def resolver(self, mode="PythonOnly"):
        resolver = Resolver(self.root, self.environment, mode)
        self.addCleanup(resolver.close)
        resolver.ready()
        return resolver

    def test_resolves_python_through_uv_subcommand(self):
        resolver = self.resolver()
        version = resolver.run(1, {"PythonVersion": {"constraints": [">=3.12"]}})
        self.assertEqual(version["result"], {"Ok": "3.12.7"})
        manifest = {
            "packages": ["six>=1"],
            "python_version": [">=3.12"],
            "exclude_newer": "2026-01-01",
        }
        prepared = resolver.run(2, {"Python": {"requirements": manifest, "r": None}})
        self.assertEqual(
            prepared["result"],
            {"Ok": {"python": sys.executable, "requirements": manifest}},
        )
        commands = [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
        ]
        invocation = commands[-1]
        self.assertEqual(
            invocation[:6],
            ["tool", "run", "--isolated", "--python", "3.12.7", "--exclude-newer"],
        )
        self.assertEqual(invocation[6:9], ["2026-01-01", "--with", "six>=1"])
        resolver.send("Close")
        self.assertEqual(resolver.receive(), "Closed")
        self.assertEqual(resolver.process.wait(timeout=10), 0)

    def test_resolves_r_through_ir_subcommand(self):
        resolver = self.resolver("R")
        self.assertEqual(resolver.run(1, "Bootstrap")["result"], {"Ok": None})
        prepared = resolver.run(2, {"R": {"requirements": ["jsonlite"]}})
        result = prepared["result"]["Ok"]
        self.assertEqual(result["library"], str(self.library))
        self.assertEqual(result["requirements"], ["jsonlite"])
        invocation = json.loads(
            self.record.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(invocation[:2], ["run", "--rscript"])
        self.assertEqual(Path(invocation[2]), self.r_home / "bin/Rscript.exe")
        self.assertEqual(invocation[3:5], ["--with", "jsonlite"])

    def test_resolver_failure_preserves_diagnostics(self):
        self.environment["TEST_RESOLVER_MODE"] = "failed"
        resolver = self.resolver()
        result = resolver.run(
            1, {"Python": {"requirements": {"packages": ["six"]}, "r": None}}
        )
        self.assertIn("fixture resolver failure", result["result"]["Err"])

    def blocked_resolver(self, mode="blocked"):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        self.addCleanup(listener.close)
        self.environment["TEST_RESOLVER_GATE"] = (
            f"127.0.0.1:{listener.getsockname()[1]}"
        )
        self.environment["TEST_RESOLVER_MODE"] = mode
        resolver = self.resolver()
        resolver.send(
            {
                "Run": {
                    "id": 1,
                    "operation": {
                        "Python": {"requirements": {"packages": ["six"]}, "r": None}
                    },
                }
            }
        )
        connection, _ = listener.accept()
        with connection, connection.makefile() as input:
            pids = [int(pid) for pid in input.readline().split()]
        return resolver, pids

    def assert_retired(self, pids):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        for pid in pids:
            handle = kernel.OpenProcess(0x100000, 0, pid)
            if handle:
                try:
                    self.assertEqual(
                        kernel.WaitForSingleObject(handle, 0),
                        0,
                        f"resolver process {pid} survived completion",
                    )
                finally:
                    kernel.CloseHandle(handle)
            else:
                self.assertEqual(ctypes.get_last_error(), 87)

    def test_interrupt_confirms_descendant_retirement(self):
        resolver, pids = self.blocked_resolver()
        resolver.send({"Control": {"id": 1, "control": "Interrupted"}})
        self.assertEqual(
            resolver.receive(), {"Controlled": {"id": 1, "result": {"Ok": True}}}
        )
        completed = resolver.receive()["Completed"]
        self.assertTrue(completed["confirmed"], completed)
        self.assertEqual(completed["control"], "Interrupted")
        self.assertIn("Err", completed["result"])
        self.assert_retired(pids)
        # A settled interrupt belongs to the old operation; a later call works.
        self.assertEqual(
            resolver.run(2, {"PythonVersion": {"constraints": []}})["result"],
            {"Ok": "3.12.7"},
        )

    def test_eof_cancels_resolver_and_descendants(self):
        resolver, pids = self.blocked_resolver()
        resolver.process.stdin.close()
        resolver.process.wait(timeout=10)
        self.assert_retired(pids)

    def test_success_retires_descendant_holding_output(self):
        resolver, pids = self.blocked_resolver("exited")
        completed = resolver.receive()["Completed"]
        self.assertTrue(completed["confirmed"], completed)
        self.assertIn("Ok", completed["result"])
        self.assert_retired(pids)


@unittest.skipUnless(os.name == "nt", "native Windows materialization")
class WindowsResolverMaterialization(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix="console materialization 日本語 "
        )
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "Windows resolver acceptance requires uv")
        shutil.copyfile(uv, self.root / "uv.exe")
        self.environment = dict(
            os.environ,
            PATH=os.pathsep.join(
                [
                    str(self.root),
                    str(Path(sys.executable).parent),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            ),
            MCP_CONSOLE_HOME=str(self.root / "home"),
            LC_ALL="C",
        )
        self.environment.pop("RETICULATE_PYTHON", None)
        self.environment.pop("RETICULATE_UV", None)

    def resolver(self, mode):
        resolver = Resolver(self.root, self.environment, mode)
        self.addCleanup(resolver.close)
        resolver.ready()
        return resolver

    def test_uv_materializes_retained_python_environment(self):
        resolver = self.resolver("PythonOnly")
        prepared = resolver.run(
            1,
            {
                "Python": {
                    "requirements": {"packages": ["six==1.17.0"]},
                    "r": None,
                    "selected_python": sys.executable,
                }
            },
        )
        candidate = prepared["result"]["Ok"]["python"]
        self.assertNotEqual(Path(candidate), Path(sys.executable))
        output = subprocess.check_output(
            [candidate, "-I", "-c", "import six; print(six.__version__)"], text=True
        )
        self.assertEqual(output.strip(), "1.17.0")

    def test_uv_bootstraps_ir_and_materializes_r_library(self):
        r_home = os.environ.get("R_HOME")
        if not r_home:
            r_home = subprocess.check_output(["R", "RHOME"], text=True).strip()
        self.environment["R_HOME"] = r_home
        resolver = self.resolver("R")
        self.assertEqual(
            resolver.run(1, "Bootstrap", timeout=180)["result"], {"Ok": None}
        )
        prepared = resolver.run(2, {"R": {"requirements": ["jsonlite"]}}, timeout=180)
        result = prepared["result"]["Ok"]
        self.assertTrue(
            (Path(result["library"]) / "jsonlite/DESCRIPTION").is_file(), result
        )
        self.assertEqual(result["requirements"], ["jsonlite"])
        # The real ir child executes the embedded multiline program with Rscript.
        self.assertTrue(prepared["confirmed"])


if __name__ == "__main__":
    unittest.main()
