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
import shutil
import subprocess
import sys
import tempfile
import time
from threading import Thread
from textwrap import dedent
import unittest

from windows_cargo import WindowsCargo  # noqa: F401 -- include build acceptance
from windows_relay import WindowsRelay  # noqa: F401 -- include protocol acceptance
from windows_resolver import (  # noqa: F401 -- include resolver acceptance
    WindowsResolver,
    WindowsResolverMaterialization,
)
from windows_sandbox import WindowsSandbox  # noqa: F401 -- include sandbox acceptance


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


class Session:
    def __init__(
        self,
        environment=None,
        *,
        relay=None,
        python=None,
        bare_r=False,
        defer_bootstrap=False,
        sandbox=False,
        overrides=(),
    ):
        if sandbox:
            from windows_sandbox import workspace

            class Directory:
                def __init__(self):
                    self.context = workspace()
                    self.name = str(self.context.__enter__())

                def cleanup(self):
                    self.context.__exit__(None, None, None)

            self.directory = Directory()
        else:
            self.directory = tempfile.TemporaryDirectory(prefix="console windows ")
        (Path(self.directory.name) / ".agents/console").mkdir(parents=True)
        self.errors = tempfile.TemporaryFile()
        command = [str(BINARY), "serve"]
        if not sandbox:
            command.append("--no-sandbox")
        for override in overrides:
            command.extend(["-c", override])
        if python is not None:
            command.extend(["-c", f"python={json.dumps(str(python))}"])
        if environment is None:
            environment = dict(os.environ, RETICULATE_PYTHON=sys.executable)
        environment = dict(
            environment, MCP_CONSOLE_HOME=str(Path(self.directory.name) / "home")
        )
        if bare_r:
            # Hiding ir/uv on PATH is insufficient when ambient reticulate can
            # bootstrap uv. Isolate package libraries for preinstalled-R cases.
            library = Path(self.directory.name) / "r-library"
            library.mkdir()
            for name in ("R_LIBS", "R_LIBS_SITE", "R_LIBS_USER"):
                environment[name] = str(library)
            environment.pop("RETICULATE_UV", None)
        self.defer_bootstrap = defer_bootstrap
        if defer_bootstrap:
            # Interrupt retryable Python setup before eager bootstrap enters R.
            # The test's first cell then chooses which runtime to finish first.
            (Path(self.directory.name) / "sitecustomize.py").write_text(
                dedent("""
                    import builtins
                    import sys

                    if sys.argv[0] == "" and "_mcp_console_services" in sys.modules:
                        if not getattr(builtins, "bootstrap_deferred", False):
                            builtins.bootstrap_deferred = True
                            input("defer bootstrap> ")
                    """)
            )
            environment["RETICULATE_PYTHONPATH"] = self.directory.name
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

    def request(self, method, params, *, close_input=False):
        self.sequence += 1
        message = {
            "jsonrpc": "2.0",
            "id": self.sequence,
            "method": method,
            "params": params,
        }
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()
        if close_input:
            self.process.stdin.close()
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

        if self.defer_bootstrap:
            result = self.send(
                python="raise AssertionError('interrupted bootstrap ran fixture cell')",
                timeout_ms=0,
            )
            deadline = time.monotonic() + self.timeout
            while (
                "waiting for stdin" not in json.dumps(result)
                and time.monotonic() < deadline
            ):
                result = self.send(timeout_ms=1000)
            assert "defer bootstrap> " in json.dumps(result), result
            assert "waiting for stdin" in json.dumps(result), result
            result = self.send(control="interrupt", timeout_ms=10_000)
            assert "KeyboardInterrupt" in json.dumps(result), result
            assert "interrupted bootstrap ran fixture cell" not in json.dumps(result), (
                result
            )
            assert "running;" not in json.dumps(result), result

    def send(self, **arguments):
        observe_completion = "timeout_ms" not in arguments
        arguments.setdefault("timeout_ms", 10000)
        result = self.request("tools/call", {"name": "send", "arguments": arguments})
        # Cold resolver caches can outlive one observation. Preserve tests that
        # explicitly choose a timeout to inspect an intermediate lifecycle state.
        if observe_completion:
            for _ in range(18):
                if not any(
                    item.get("text", "")
                    .strip()
                    .endswith("[running; poll with an empty send]")
                    for item in result.get("content", [])
                ):
                    break
                result = self.request(
                    "tools/call", {"name": "send", "arguments": {"timeout_ms": 10000}}
                )
        return result

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
            for source in (
                "build_backend.py",
                "windows_checkout.py",
                "tests/fixtures/windows_build.py",
            ):
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
    def test_consumes_idle_interrupt_before_next_cell(self):
        # Exercise the same public contract as the Unix Python-only case,
        # including both runtime initialization orders and a shared send.
        for order in ("python", "python-r", "r-python"):
            with self.subTest(order=order):
                session = Session(defer_bootstrap=True)
                try:
                    session.initialize()
                    if order == "r-python":
                        self.assertIn("42", json.dumps(session.send(r="42L")))
                    self.assertFalse(
                        session.send(python="import time; retained = 0")["isError"]
                    )
                    if order == "python-r":
                        self.assertIn("42", json.dumps(session.send(r="42L")))
                    for expected in range(1, 41):
                        control = {"control": "interrupt"}
                        if expected % 2:
                            result = session.send(**control)
                            if order != "python":
                                # Empty polls do not dispatch worker commands.
                                # R must acknowledge while idle, even if the
                                # interrupt call's grace ended before it ran.
                                deadline = time.monotonic() + 5
                                while (
                                    result["content"]
                                    == [{"type": "text", "text": "\n[idle]"}]
                                    and time.monotonic() < deadline
                                ):
                                    time.sleep(0.01)
                                    result = session.send()
                                self.assertEqual(
                                    result["content"],
                                    [{"type": "text", "text": "\n\n[idle]"}],
                                )
                            control = {}
                        result = session.send(
                            **control,
                            python="time.sleep(0.01); retained += 1; print(retained)",
                        )
                        output = "".join(item["text"] for item in result["content"])
                        self.assertFalse(result["isError"], result)
                        wanted = f"{expected}\n" + ("[done]" if control else "")
                        if control and order != "python":
                            # R's acknowledgment may reach the server before
                            # or after it admits the same-call following cell.
                            self.assertIn(
                                output,
                                (
                                    "\n" + wanted,
                                    "\n[output produced while idle]\n" + wanted,
                                ),
                            )
                        else:
                            self.assertEqual(output, wanted)
                    if order != "python":
                        result = session.send(control="interrupt", r="42L")
                        self.assertIn("[1] 42", json.dumps(result))
                        self.assertFalse(result["isError"], result)
                finally:
                    session.close()

    def test_interrupt_and_following_cell_keep_input_and_state_separate(self):
        session = self.session(defer_bootstrap=True)
        session.send(python="history = []")
        for initialize_r in (False, True):
            if initialize_r:
                session.send(r="42L")
            result = session.send(
                python="history.append('old'); input('Old input: '); history.append('wrong')"
            )
            self.assertIn("waiting for stdin", json.dumps(result))
            result = session.send(
                control="interrupt",
                python="history.append('new'); value = input('New input: '); history.append(value)",
            )
            output = json.dumps(result)
            self.assertIn("KeyboardInterrupt", output)
            self.assertIn("New input: ", output)
            self.assertIn("waiting for stdin", output)
            result = session.send(stdin="delivered to new cell\n")
            self.assertNotIn("waiting for stdin", json.dumps(result))
            expected = ["old", "new", "delivered to new cell"] * (1 + initialize_r)
            result = session.send(python="print(history)")
            self.assertEqual(
                result["content"], [{"type": "text", "text": repr(expected) + "\n"}]
            )

    def test_rejects_startup_environment_mutation(self):
        with tempfile.TemporaryDirectory(prefix="console startup ") as directory:
            (Path(directory) / "sitecustomize.py").write_text(
                "import sys; sys.prefix = 'changed-by-startup-hook'\n"
            )
            session = Session(
                dict(
                    os.environ,
                    RETICULATE_PYTHON=sys.executable,
                    RETICULATE_PYTHONPATH=directory,
                )
            )
            try:
                session.initialize()
                result = session.send(
                    python="raise AssertionError('invalid environment ran code')"
                )
                output = json.dumps(result)
                self.assertTrue(result["isError"], result)
                self.assertIn(
                    "RuntimeError: embedded Python prefix differs from the selected environment",
                    output,
                )
                self.assertNotIn("FileNotFoundError", output)
                self.assertNotIn("invalid environment ran code", output)
            finally:
                session.close()

    def test_empty_python_selection_uses_available_runtimes(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "Windows resolver acceptance requires uv")
        r_home = (
            os.environ.get("R_HOME")
            or subprocess.check_output(["R", "RHOME"], text=True).strip()
        )
        system_path = str(Path(os.environ["SystemRoot"]) / "System32")
        for with_python in (False, True):
            with self.subTest(with_python=with_python):
                environment = dict(
                    os.environ,
                    R_HOME=r_home,
                    RETICULATE_PYTHON="",
                    PATH=(
                        str(Path(sys.executable).parent) + os.pathsep
                        if with_python
                        else ""
                    )
                    + system_path,
                )
                if with_python:
                    # The managed pair needs a resolver even with a restricted
                    # PATH; do not depend on reticulate's ambient uv cache.
                    environment.update(
                        RETICULATE_UV=uv,
                        UV_PYTHON_PREFERENCE="only-system",
                        UV_PYTHON_DOWNLOADS="never",
                    )
                session = Session(environment, bare_r=not with_python)
                try:
                    session.initialize()
                    self.assertIn("42", json.dumps(session.send(r="42L")))
                    if with_python:
                        self.assertIn("43", json.dumps(session.send(python="43")))
                finally:
                    session.close()

    def test_eof_preserves_first_send_response(self):
        # A custom worker keeps this independent of runtime preparation, which
        # EOF may cancel. The malformed call never needs to launch the worker.
        session = Session(relay=BINARY)
        self.addCleanup(session.close)
        session.initialize()
        session.request("tools/list", {})
        # Span multiple input chunks so physical EOF can arrive while the
        # asynchronous transport is still collecting the complete request.
        result = session.request(
            "tools/call",
            {"name": "send", "arguments": {"r": "1" + " " * 131072, "python": "1"}},
            close_input=True,
        )
        self.assertEqual(
            result,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "only one of `r`, `python`, or `sql` may be supplied",
                    }
                ],
                "isError": True,
            },
        )
        self.assertEqual(session.process.wait(timeout=10), 0)

    def test_r_without_python(self):
        # Capture R before removing the interpreter launchers from PATH.
        r_home = (
            os.environ.get("R_HOME")
            or subprocess.check_output(["R", "RHOME"], text=True).strip()
        )
        environment = dict(
            os.environ,
            R_HOME=r_home,
            PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
        )
        environment.pop("RETICULATE_PYTHON", None)
        session = Session(environment, bare_r=True)
        self.addCleanup(session.close)
        session.initialize()
        self.assertIn("42", json.dumps(session.send(r="answer <- 42L; answer")))
        self.assertIn("42", json.dumps(session.send(r="answer")))
        self.assertIn(
            "FALSE", json.dumps(session.send(control="restart", r="exists('answer')"))
        )

    def test_python_sleep_interrupt(self):
        session = self.session(defer_bootstrap=True)
        session.send(python="import time; saved = 42")
        for initialize_r in (False, True):
            with self.subTest(initialize_r=initialize_r):
                if initialize_r:
                    self.assertIn("42", json.dumps(session.send(r="42")))
                self.assertIn(
                    "running;",
                    json.dumps(session.send(python="time.sleep(60)", timeout_ms=100)),
                )
                result = session.send(control="interrupt", timeout_ms=2000)
                self.assertIn("KeyboardInterrupt", json.dumps(result))
                self.assertNotIn("running;", json.dumps(result))
                self.assertIn("42", json.dumps(session.send(python="saved")))

    def test_interrupt_then_input_after_r_attachment(self):
        session = self.session(defer_bootstrap=True)
        self.assertIn("42", json.dumps(session.send(python="saved = 42; saved")))
        self.assertIn(
            "running;",
            json.dumps(session.send(python="while True: pass", timeout_ms=100)),
        )
        self.assertIn(
            "KeyboardInterrupt",
            json.dumps(session.send(control="interrupt", timeout_ms=2000)),
        )
        self.assertIn("42", json.dumps(session.send(r="42L")))
        # A failed iteration leaves an active cell: stop at the first failure.
        for _ in range(12):
            result = session.send(r="repeat { Sys.sleep(0) }", timeout_ms=50)
            self.assertIn("running;", json.dumps(result))
            result = session.send(control="interrupt", timeout_ms=2000)
            self.assertNotIn("running;", json.dumps(result))
            for arguments in (
                {"r": "readline('R input: ')"},
                {"python": "input('Python input: ')"},
            ):
                result = session.send(**arguments, timeout_ms=50)
                deadline = time.monotonic() + 10
                while "running;" in json.dumps(result) and time.monotonic() < deadline:
                    result = session.send(timeout_ms=1000)
                self.assertIn("waiting for stdin", json.dumps(result))
                result = session.send(control="interrupt", timeout_ms=2000)
                self.assertNotIn("running;", json.dumps(result))
                self.assertNotIn("waiting for stdin", json.dumps(result))
            self.assertIn("42", json.dumps(session.send(python="saved")))

    def test_python_without_r(self):
        environment = dict(
            os.environ,
            PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
            RETICULATE_PYTHON=sys.executable,
        )
        environment.pop("R_HOME", None)
        session = Session(environment)
        self.addCleanup(session.close)
        session.initialize()
        # fmt: python
        result = session.send(
            python=dedent("""
                import ctypes

                assert ctypes.windll.kernel32.GetModuleHandleW("R.dll") == 0
                answer = 42
                print("Python alone:", answer)
                """)
        )
        self.assertIn("Python alone: 42", json.dumps(result))
        self.assertFalse(result.get("isError"), result)
        result = session.send(
            python="name = input('Name: '); print(name)", timeout_ms=100
        )
        self.assertIn("waiting for stdin", json.dumps(result))
        self.assertIn(
            "caf\u00e9",
            json.dumps(session.send(stdin="caf\u00e9\n"), ensure_ascii=False),
        )
        result = session.send(python="while True: pass", timeout_ms=100)
        self.assertIn("running;", json.dumps(result))
        result = session.send(control="interrupt", timeout_ms=2000)
        self.assertIn("KeyboardInterrupt", json.dumps(result))
        self.assertNotIn("running;", json.dumps(result))
        self.assertIn("42", json.dumps(session.send(python="answer")))
        result = session.send(r="42")
        self.assertTrue(result.get("isError"), result)
        self.assertIn("R cells are unavailable", json.dumps(result))
        result = session.send(
            control="restart", python="print('fresh', 'answer' in globals())"
        )
        self.assertIn("fresh False", json.dumps(result))

    def test_r_initializes_before_python(self):
        session = self.session(defer_bootstrap=True)
        # fmt: r
        result = session.send(
            r=dedent("""
                r_value <- 41L
                stopifnot(!reticulate::py_available(initialize = FALSE))
                cat("R without Python")
                """)
        )
        self.assertIn("R without Python", json.dumps(result))
        result = session.send(python="python_value = 42; print(r.r_value)")
        self.assertIn("41", json.dumps(result))
        self.assertNotIn("Traceback", json.dumps(result))
        result = session.send(
            r="stopifnot(py$python_value == 42L); cat('bridge ready')"
        )
        self.assertIn("bridge ready", json.dumps(result))

    def test_selected_virtualenv_with_unicode_path(self):
        directory = tempfile.TemporaryDirectory(prefix="console Python \u03bb ")
        self.addCleanup(directory.cleanup)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", directory.name], check=True
        )
        executable = Path(directory.name) / "Scripts/python.exe"
        session = Session(python=executable)
        self.addCleanup(session.close)
        session.initialize()
        # fmt: python
        source = dedent("""
            import os, sys, subprocess, json

            assert sys.prefix != sys.base_prefix
            child = json.loads(
                subprocess.check_output(
                    [sys.executable, "-c", "import sys, json; print(json.dumps(sys.prefix))"],
                    text=True,
                )
            )
            assert os.path.samefile(sys.prefix, child)
            print("virtualenv retained")
            """)
        self.assertIn("virtualenv retained", json.dumps(session.send(python=source)))
        self.assertIn(
            "virtualenv retained",
            json.dumps(session.send(control="restart", python=source)),
        )

    def test_selected_python_rejects_managed_requirements(self):
        environment = dict(
            os.environ,
            PATH=str(Path(os.environ["SystemRoot"]) / "System32"),
            RETICULATE_PYTHON=sys.executable,
        )
        environment.pop("R_HOME", None)
        session = Session(environment)
        self.addCleanup(session.close)
        session.initialize()
        schema = session.request("tools/list", {})
        tool = schema["tools"][0]
        properties = tool["inputSchema"]["properties"]
        self.assertNotIn("sql", properties)
        self.assertIn("ir and uv", tool["description"])
        self.assertIn("local execution on Windows", tool["description"])
        for language in ("r", "python"):
            with self.subTest(language=language):
                description = properties[language]["description"].lower()
                self.assertNotIn("sql", description)
                self.assertNotIn("duckdb", description)
                self.assertIn("resolution", description)
        self.assertEqual(
            schema["tools"][0]["inputSchema"]["properties"]["requirements"][
                "properties"
            ]["action"]["enum"],
            ["get", "add", "set", "reset"],
        )
        self.assertIn("initialize in the background", tool["description"])
        control = properties["control"]["description"]
        self.assertIn("cooperative interrupt", control)
        self.assertNotIn("SIGINT", control)
        result = session.send(requirements={"action": "add", "python": ["six"]})
        self.assertTrue(result.get("isError"), result)
        self.assertIn("unavailable", json.dumps(result))
        self.assertIn("42", json.dumps(session.send(python="42")))

    def test_managed_python_requirements_without_r(self):
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "Windows resolver acceptance requires uv")
        directory = tempfile.TemporaryDirectory(prefix="console managed 日本語 ")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        shutil.copyfile(uv, root / "uv.exe")
        environment = dict(
            os.environ,
            PATH=os.pathsep.join(
                [
                    str(root),
                    str(Path(sys.executable).parent),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            ),
            RETICULATE_PYTHON="managed",
            UV_PYTHON_PREFERENCE="only-system",
            UV_PYTHON_DOWNLOADS="never",
        )
        environment.pop("R_HOME", None)
        session = Session(environment)
        self.addCleanup(session.close)
        session.initialize()

        def completed(**arguments):
            result = session.send(**arguments)
            self.assertFalse(result.get("isError"), result)
            return result

        result = completed(
            requirements={"action": "set", "python": ["six"]},
            python="import six; saved = 42; print(six.__version__)",
        )
        self.assertIn("1.", json.dumps(result))
        result = completed(python="import sniffio; print(sniffio.__version__)")
        self.assertIn("1.", json.dumps(result))
        inspection = session.send(requirements={"action": "get"})
        self.assertEqual(
            inspection["structuredContent"]["requirements"]["python"],
            ["six", "sniffio"],
        )
        failed = session.send(
            control="restart",
            requirements={"action": "set", "python": ["six==0"]},
        )
        self.assertTrue(failed.get("isError"), failed)
        inspection = session.send(requirements={"action": "get"})
        self.assertEqual(
            inspection["structuredContent"]["requirements"]["python"],
            ["six", "sniffio"],
        )
        self.assertIn("42", json.dumps(completed(python="print(saved)")))
        result = completed(
            control="restart", python="import six, sniffio; print('saved' in globals())"
        )
        self.assertIn("False", json.dumps(result))

    def test_managed_r_requirements_preserve_live_state(self):
        session = self.session()
        self.assertIn("42", json.dumps(session.send(r="saved <- 42L; saved")))
        result = session.send(
            requirements={"r": ["digest"]},
            r="stopifnot(saved == 42L); digest::digest('prepared')",
        )
        self.assertFalse(result.get("isError"), result)
        inspection = session.send(requirements={"action": "get"})
        self.assertIn("digest", inspection["structuredContent"]["requirements"]["r"])
        result = session.send(
            control="restart",
            r="stopifnot(!exists('saved')); digest::digest('prepared')",
        )
        self.assertFalse(result.get("isError"), result)

    def test_python_initializes_before_r(self):
        session = Session(defer_bootstrap=True)
        self.addCleanup(session.close)
        session.initialize()
        # fmt: python
        result = session.send(
            python=dedent("""
                import ctypes
                import sys

                assert ctypes.windll.kernel32.GetModuleHandleW("R.dll") == 0
                python_value = 41
                print("python without R")
                """)
        )
        self.assertFalse(result.get("isError"), result)
        self.assertIn("python without R", json.dumps(result))
        self.assertNotIn("AssertionError", json.dumps(result))
        self.assertIn("42", json.dumps(session.send(r="r_value <- 42; r_value")))
        self.assertIn("42", json.dumps(session.send(python="print(python_value + 1)")))
        self.assertIn("42", json.dumps(session.send(r="r_value")))
        self.assertIn(
            "41", json.dumps(session.send(r="reticulate::py_eval('python_value')"))
        )
        self.assertIn("41", json.dumps(session.send(r="py$python_value")))
        self.assertIn("42", json.dumps(session.send(python="r.r_value")))

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

    def test_restart_while_relay_exits_on_shutdown(self):
        session = self.relay_session("exit_shutdown")
        self.assertIn("42", json.dumps(session.send(r="42")))
        for generation in range(12):
            with self.subTest(generation=generation):
                result = session.send(control="restart", r="42", timeout_ms=10000)
                self.assertFalse(result.get("isError"), result)
                self.assertIn("42", json.dumps(result))

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

    def test_startup_eof_cancels_python_inspection(self):
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
                ["rustc", str(source), "-o", str(root / "python.exe")], check=True
            )
            environment = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                RETICULATE_PYTHON=str(root / "python.exe"),
                TEST_RESOLVER_PID=str(root / "child.pid"),
                TEST_READY_ADDR=f"127.0.0.1:{ready.getsockname()[1]}",
            )
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

    def test_python_inspection_descendants_are_retired(self):
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
                ["rustc", str(source), "-o", str(root / "python.exe")], check=True
            )
            environment = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                RETICULATE_PYTHON=str(root / "python.exe"),
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
                self.assertTrue(result.get("isError"), result)
                self.assertIn(
                    "invalid selected Python configuration", json.dumps(result)
                )
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

    def session(self, *, defer_bootstrap=False):
        session = Session(defer_bootstrap=defer_bootstrap)
        self.addCleanup(session.close)
        session.initialize()
        return session

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
            session = Session(
                dict(os.environ, R_USER=user_home, RETICULATE_PYTHON=sys.executable)
            )
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
                          identical(
                            normalizePath(path.expand("~")),
                            normalizePath(Sys.getenv("R_USER"))
                          )
                        )
                        loadNamespace("splines")
                        stopifnot(file.exists(system.file("DESCRIPTION", package = "splines")))
                        cat("startup paths intact")
                        """).strip()
                )
                self.assertFalse(result.get("isError"), result)
                self.assertIn("startup paths intact", json.dumps(result))

    def test_python_errors_preserve_state(self):
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

    def test_python_plots_and_interrupt(self):
        environment = dict(os.environ)
        environment["RETICULATE_PYTHON"] = sys.executable
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
