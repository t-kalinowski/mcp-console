"""Public Windows configuration and explicitly provisioned sandbox acceptance."""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from textwrap import dedent
from support.installation import native_console
from support.normalization import code
from support.public_configuration import INVALID_CONFIGURATIONS

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@contextmanager
def workspace(parent: Path | None = None):
    # Inherit the user's ordinary ACLs. Python 3.14's private temp directories
    # allow only owner/admin/system, which a restricted token cannot traverse.
    root = (
        parent or Path(tempfile.gettempdir())
    ) / f"console sandbox {uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        # Windows can retain an executable's image mapping briefly after its
        # waited-for process exits. Retry only that sharing violation.
        deadline = time.monotonic() + 5
        while True:
            try:
                shutil.rmtree(root)
                break
            except OSError as error:
                if error.winerror != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


@unittest.skipUnless(os.name == "nt", "native Windows sandbox")
class WindowsSandbox(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("R_HOME")
        and os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "configured R and provisioned default Windows sandbox",
    )
    def test_native_r_startup_is_contained_and_reread_on_restart(self):
        from windows import Session

        with workspace() as root:
            home = root / "home"
            home.mkdir()
            environ = home / ".Renviron"
            environ.write_text("CONSOLE_NATIVE_STARTUP=first\n")
            profile = home / ".Rprofile"
            profile.write_text(
                # fmt: r
                code("""
                    native_value <- Sys.getenv("CONSOLE_NATIVE_STARTUP")
                    native_allowed <- file.path(tempdir(), "native-allowed")
                    writeLines(native_value, native_allowed)
                    native_blocked <- tryCatch(
                      {
                        suppressWarnings(writeLines(
                          "forbidden",
                          Sys.getenv("CONSOLE_NATIVE_FORBIDDEN")
                        ))
                        FALSE
                      },
                      error = function(error) TRUE
                    )
                    stopifnot(native_blocked)
                    options(width = 73L)
                    .First <- function() cat("native Windows startup\\n")
                    """)
            )
            environment = dict(
                os.environ,
                HOME=str(home),
                R_USER=str(home),
                R_ENVIRON=os.devnull,
                R_PROFILE=os.devnull,
                R_ENVIRON_USER=str(environ),
                R_PROFILE_USER=str(profile),
                MCP_CONSOLE_LANGUAGES="r",
                CONSOLE_NATIVE_FORBIDDEN=str(root / "forbidden"),
            )
            session = Session(environment, sandbox=True, use_r_startup_files=True)
            try:
                session.initialize()
                # fmt: r
                check = code("""
                    stopifnot(
                      native_blocked,
                      file.exists(native_allowed),
                      identical(getOption("width"), 73L),
                      identical(readLines(native_allowed), native_value)
                    )
                    cat(native_value)
                    """)
                self.assertIn("first", json.dumps(session.send(r=check)))
                self.assertFalse((root / "forbidden").exists())
                environ.write_text("CONSOLE_NATIVE_STARTUP=edited\n")
                result = session.send(control="restart", r=check)
                self.assertIn("edited", json.dumps(result))
                self.assertFalse((root / "forbidden").exists())
            finally:
                session.close()

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_console_cache_paths_reach_resolver_and_worker(self):
        self.console_cache_paths(profile_fallback=False)

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_console_cache_falls_back_to_userprofile_after_relative_home(self):
        self.console_cache_paths(profile_fallback=True)

    def console_cache_paths(self, *, profile_fallback: bool):
        from windows import Session

        with workspace() as root:
            selected = root / "python"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", selected], check=True
            )
            # Hosted temp directories may exclude the sandbox account. Grant
            # reads for the selected venv, session cwd, and host-written probe.
            status = json.loads(
                subprocess.check_output(
                    [BINARY, "sandbox-setup", "--status"], text=True
                )
            )
            self.assertTrue(status["configured"], status)
            subprocess.run(
                [
                    "icacls",
                    str(root),
                    "/grant",
                    f"{status['online_account']}:(OI)(CI)RX",
                ],
                check=True,
                capture_output=True,
            )
            python = selected / "Scripts/python.exe"
            local = (
                root / "account/AppData/Local" if profile_fallback else root / "local"
            )
            cache = local / "mcp-console/cache/dependencies"
            environment = dict(
                os.environ,
                PATH=str(root),
                LOCALAPPDATA=str(local),
                UV_CACHE_DIR=str(root / "host-uv"),
                IR_CACHE_DIR=str(root / "host-ir"),
                CACHE_TEST_ROOT=str(cache),
            )
            for name in ("XDG_CACHE_HOME", "R_HOME", "RETICULATE_PYTHON"):
                environment.pop(name, None)
            if profile_fallback:
                environment.pop("LOCALAPPDATA")
                environment.update(
                    HOME="relative-home", USERPROFILE=str(root / "account")
                )
            (selected / "Lib/site-packages/sitecustomize.py").write_text(
                dedent("""
                    import os
                    from pathlib import Path

                    if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
                        root = Path(os.environ["CACHE_TEST_ROOT"])
                        cache = Path(os.environ["UV_CACHE_DIR"])
                        assert cache.is_relative_to(root)
                        cache.mkdir(parents=True, exist_ok=True)
                        cache.joinpath("resolver-probe").write_text("prepared")
                    """)
            )
            # Keep the cwd under the explicit read grant too. The native
            # runner's background read-ACL traversal can still be in progress.
            session = Session(
                os.environ,
                python=python,
                sandbox=True,
                temporary_root=root,
                overrides=[
                    'sandbox.network="enabled"',
                    "inherit_environment=false",
                    "environment=" + json.dumps(environment),
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    python=dedent("""
                        import os
                        from pathlib import Path

                        root = Path(os.environ["CACHE_TEST_ROOT"])
                        for name in ("UV_CACHE_DIR", "IR_CACHE_DIR", "R_USER_CACHE_DIR", "RENV_PATHS_CACHE"):
                            assert Path(os.environ[name]).is_relative_to(root)
                        assert Path(os.environ["UV_CACHE_DIR"]).joinpath("resolver-probe").read_text() == "prepared"
                        print("Console caches retained")
                        """)
                )
                self.assertIn("Console caches retained", json.dumps(result), result)
            finally:
                session.close()
            self.assertFalse((root / "host-uv").exists())
            self.assertFalse((root / "host-ir").exists())

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_network_enabled_sql(self):
        from windows import Session, exercise_r_sql

        session = Session(sandbox=True, overrides=['sandbox.network="enabled"'])
        try:
            session.initialize()
            exercise_r_sql(session)
        finally:
            session.close()

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_network_enabled_python_sql(self):
        from windows import Session, exercise_sql_interrupt
        from support.resolvers import expose_uv

        with workspace() as root:
            expose_uv(root)
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
                UV_PYTHON_PREFERENCE="system",
                UV_PYTHON_DOWNLOADS="never",
            )
            environment.pop("R_HOME", None)
            session = Session(
                environment,
                sandbox=True,
                overrides=['sandbox.network="enabled"'],
            )
            try:
                session.initialize()
                session.request("tools/list", {})
                session.expect("1234567", sql="SELECT 1234567 AS answer")
                session.expect(
                    "Count", sql="CREATE TABLE retained AS SELECT 1234567 AS answer"
                )
                exercise_sql_interrupt(session)
                session.expect(
                    "1234567", control="restart", sql="SELECT 1234567 AS answer"
                )
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_network_enabled_input_and_interrupt(self):
        from windows import Session, exercise_input_and_interrupt

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                exercise_input_and_interrupt(session)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_network_enabled_idle_later_callbacks(self):
        from windows import Session, exercise_later_callbacks

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                exercise_later_callbacks(session)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "explicitly provisioned elevated Windows sandbox",
    )
    def test_provisioned_input_and_interrupt_with_private_installation(self):
        # A private bundle outside the user's profile models hosted CI's
        # nonstandard installation paths, without relying on another drive.
        public = Path(os.environ["PUBLIC"]).resolve()
        with tempfile.TemporaryDirectory(
            prefix="console-ci-assets-", dir=public
        ) as directory:
            prefix = Path(directory).resolve()
            self.assertTrue(prefix.is_relative_to(public))
            owner = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
            subprocess.run(
                [
                    "icacls",
                    str(prefix),
                    "/inheritance:r",
                    "/grant:r",
                    f"{owner}:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
            )
            source = native_console(BINARY)
            (prefix / "bin").mkdir()
            binary = prefix / "bin/mcp-console.exe"
            shutil.copy2(source, binary)
            for relative in ("libexec", "share"):
                assets = source.resolve().parent.parent / relative
                if assets.exists():
                    shutil.copytree(assets, prefix / relative)
            prepared = subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts/prepare-windows-tests",
                    "--binary",
                    binary,
                    "--state-dir",
                    os.environ["MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"],
                    "--github-env",
                    prefix / "github-env",
                    "--read-root",
                    prefix,
                    "--require-configured",
                ],
                cwd=ROOT,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(prepared.returncode, 0, (prepared.stdout, prepared.stderr))
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "tests/windows.py",
                    "WindowsSandbox.test_provisioned_network_restricted_input_and_interrupt",
                ],
                cwd=ROOT,
                env=os.environ
                | {
                    "MCP_CONSOLE_TEST_BINARY": str(binary),
                    "MCP_CONSOLE_TEST_NATIVE_BINARY": str(binary),
                },
                capture_output=True,
                timeout=240,
            )
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "explicitly provisioned elevated Windows sandbox",
    )
    def test_provisioned_network_restricted_input_and_interrupt(self):
        from windows import Session, exercise_input_and_interrupt

        session = Session(
            sandbox=True,
            overrides=[
                'sandbox.network="restricted"',
            ],
        )
        try:
            session.initialize()
            exercise_input_and_interrupt(session)
        finally:
            session.close()

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_network_enabled_allows_loopback_exchange(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                # Complete runtime startup before starting the networking deadline.
                session.expect("[done]", python="pass")
                # This socket tests networking itself; ordinary sequencing uses
                # public stdin or host-only named pipes.
                with socket.create_server(("127.0.0.1", 0)) as listener:
                    listener.settimeout(10)
                    session.send(
                        # fmt: python
                        python=dedent(f"""
                            import socket

                            with socket.create_connection(
                                ("127.0.0.1", {listener.getsockname()[1]}), timeout=10
                            ) as peer:
                                peer.sendall(b"loopback request")
                                with peer.makefile("rb") as response:
                                    print(response.read().decode())
                            """),
                        timeout_ms=0,
                    )
                    with listener.accept()[0] as peer:
                        peer.settimeout(10)
                        request = peer.makefile("rb")
                        with request:
                            self.assertEqual(request.read(16), b"loopback request")
                        peer.sendall(b"loopback reply")
                    result = session.expect("loopback reply")
                    self.assertFalse(result.get("isError"), result)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_r_uses_private_storage_and_preserves_state(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    r=dedent("""
                        value <- 40L
                        path <- file.path(tempdir(), "allowed.txt")
                        writeLines("private", path)
                        cat(value + 2L, readLines(path))
                        """)
                )
                self.assertIn("42 private", json.dumps(result), result)
                result = session.send(r="value + 3L")
                self.assertIn("43", json.dumps(result))
                result = session.send(control="restart", r='exists("value")')
                self.assertIn("FALSE", json.dumps(result))
            finally:
                session.close()

    def test_setup_help_describes_provisioned_resources(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [str(BINARY), "sandbox-setup", flag],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for detail in (
                    "McpConsoleSandboxOff",
                    "McpConsoleSandboxOn",
                    "ConsoleSandboxUsers",
                    "Windows Firewall",
                    "loopback",
                    ".sandbox-secrets",
                    ".sandbox-bin",
                    "UAC",
                    "reused without changes",
                    "%LOCALAPPDATA%",
                ):
                    self.assertIn(detail, result.stdout)

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "explicitly provisioned elevated Windows sandbox",
    )
    def test_current_setup_reports_resources_without_reprovisioning(self):
        state = os.environ["MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"]
        command = [str(BINARY), "sandbox-setup", "--state-dir", state]
        before = subprocess.run(
            [*command, "--status"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertTrue(json.loads(before.stdout)["configured"])
        # Refuse to provision an unconfigured machine in acceptance tests.
        # A current setup must reuse its records and credentials without UAC.
        records = {
            path: path.read_bytes()
            for directory in (".sandbox", ".sandbox-secrets")
            for path in (Path(state) / directory).rglob("*")
            if path.is_file()
        }
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Console sandbox is configured at {state}", result.stdout)
        for detail in (
            "McpConsoleSandboxOff",
            "McpConsoleSandboxOn",
            "ConsoleSandboxUsers",
            "Windows Firewall",
            "loopback",
            ".sandbox-secrets",
            ".sandbox-bin",
            "reused without changes",
        ):
            self.assertIn(detail, result.stdout)
        for path, content in records.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        after = subprocess.run(
            [*command, "--status"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))

    def test_setup_status_is_read_only(self):
        with workspace() as root:
            state = root / "state"
            result = subprocess.run(
                [str(BINARY), "sandbox-setup", "--status", "--state-dir", str(state)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            status = json.loads(result.stdout)
            self.assertFalse(status["configured"])
            self.assertTrue(status["helpers_available"])
            self.assertFalse(state.exists())

    def test_unconfigured_elevated_sandbox_requires_explicit_setup(self):
        with workspace() as root:
            policy = {
                "extends": ":read-only",
                "windows_state_dir": str(root / "state"),
            }
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    "cmd.exe",
                    "/d",
                    "/c",
                    "echo launched",
                ],
                cwd=root,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(policy),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"Windows sandbox setup is required", result.stderr)
            self.assertIn(b"mcp-console-sandbox setup", result.stderr)
            self.assertFalse((root / "state").exists())

    def test_modified_helper_is_rejected_before_setup(self):
        with workspace() as root:
            native = native_console(BINARY)
            source = native.resolve().parent.parent
            (root / "bin").mkdir()
            shutil.copy2(native, root / "bin/mcp-console.exe")
            for directory in ("libexec", "share"):
                if (source / directory).exists():
                    shutil.copytree(source / directory, root / directory)
            helpers = list((root / "libexec").rglob("mcp-console-sandbox-runner.exe"))
            self.assertTrue(helpers)
            for helper in helpers:
                with helper.open("ab") as stream:
                    stream.write(b"modified")
            result = subprocess.run(
                [str(root / "bin/mcp-console.exe"), "sandbox-setup", "--status"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(
                b"private artifact does not match this installation", result.stderr
            )

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_python_state_and_restart_with_private_temporary_storage(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    python=dedent("""
                        import os, tempfile
                        answer = 41
                        print(answer + 1, os.environ['MCP_CONSOLE_SANDBOX'])
                        print(tempfile.gettempdir())
                        """)
                )
                self.assertNotIn('"isError": true', json.dumps(result), result)
                self.assertIn("42 1", json.dumps(result))
                result = session.send(
                    python=dedent("""
                    print(answer + 2)
                    open('denied.txt', 'w')
                    """)
                )
                self.assertIn("43", json.dumps(result))
                self.assertIn("PermissionError", json.dumps(result))
                self.assertFalse((Path(session.directory.name) / "denied.txt").exists())
                result = session.send(
                    python=dedent("""
                        import subprocess, sys
                        child = subprocess.Popen(
                            [sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        print('DESCENDANT', child.pid)
                        """)
                )
                import re

                pid = int(re.search(r"DESCENDANT (\d+)", json.dumps(result)).group(1))
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(handle)
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                    result = session.send(
                        control="restart", python="print('answer' in globals())"
                    )
                    self.assertIn("False", json.dumps(result))
                    self.assertEqual(
                        kernel.WaitForSingleObject(handle, 0),
                        0,
                        "restart preceded descendant retirement",
                    )
                finally:
                    kernel.CloseHandle(handle)
            finally:
                session.close()

    @unittest.skipUnless(
        os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"),
        "provisioned default Windows sandbox",
    )
    def test_shared_public_policies(self):
        from support.public_configuration import CONFIGURATION_EXAMPLES

        with workspace() as root:
            for name in ("data", "secrets"):
                (root / name).mkdir()
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            for name, document in CONFIGURATION_EXAMPLES.items():
                with self.subTest(name=name):
                    config.write_text(json.dumps(document))
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "--",
                            sys.executable,
                            "-c",
                            "print('launched')",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if isinstance(document["sandbox"].get("network"), dict):
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, "")
                        self.assertIn(
                            "managed proxies are not supported", result.stderr
                        )
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "launched")

    def test_rejects_unsupported_public_permissions(self):
        with workspace() as root:
            for path, value in INVALID_CONFIGURATIONS:
                with self.subTest(path=path):
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "-c",
                            path + "=" + json.dumps(value),
                            "--",
                            "unused",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("configuration", result.stderr)
                    self.assertNotIn("secret sentinel", result.stderr)
                    self.assertNotIn("654987123", result.stderr)
            for override, diagnostic in (
                ("sandbox.network={proxy: {}}", "sandbox.network.proxy"),
                ("resolver.sandbox.network=restricted", "resolver.sandbox"),
                ("resolver.sandbox.filesystem={}", "resolver.sandbox"),
                ("sandbox.windows_sandbox_level=unelevated", "windows_sandbox_level"),
                ("sandbox.windows_state_dir=/tmp/state", "windows_state_dir"),
            ):
                with self.subTest(override=override):
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "-c",
                            override,
                            "--",
                            "cmd.exe",
                            "/c",
                            "echo launched",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(diagnostic, result.stderr)

    def test_stdio_write_policy_and_exit_status(self):
        with workspace() as root:
            work = root / "work"
            work.mkdir()
            (work / "input.txt").write_text("readable", encoding="utf-8")
            config = {
                "filesystem": {
                    "kind": "restricted",
                    "entries": [
                        {
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "read",
                        },
                        {
                            "path": {"type": "path", "path": str(work)},
                            "access": "write",
                        },
                    ],
                },
                "network": "enabled",
                "windows_sandbox_level": "unelevated",
                "windows_state_dir": str(root / "state"),
            }
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/v:on",
                    "/c",
                    "set /p INPUT=&echo !INPUT!&type input.txt&echo allowed>allowed.txt&echo denied>..\\denied.txt&exit /b 23",
                ],
                cwd=work,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(config),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                input=b"hello from stdin\r\n",
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 23, result.stderr.decode(errors="replace")
            )
            self.assertIn(b"hello from stdin", result.stdout)
            self.assertIn(b"readable", result.stdout)
            self.assertEqual((work / "allowed.txt").read_text().strip(), "allowed")
            self.assertFalse((root / "denied.txt").exists())
            # Preserve the high bit through the installed launcher as well as
            # the native runner/frontend's full-width Windows exit status.
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/c",
                    "exit /b -1073741819",
                ],
                cwd=work,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(config),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0xC0000005, result.stderr)


if __name__ == "__main__":
    unittest.main()
