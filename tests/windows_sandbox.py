"""Public Windows sandbox acceptance; no elevated account provisioning."""

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@contextmanager
def workspace():
    # Inherit the user's ordinary ACLs. Python 3.14's private temp directories
    # allow only owner/admin/system, which a restricted token cannot traverse.
    root = Path(tempfile.gettempdir()) / f"console sandbox {uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


@unittest.skipUnless(os.name == "nt", "native Windows sandbox")
class WindowsSandbox(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_r_uses_private_storage_and_preserves_state(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.windows_sandbox_level="restricted-token"',
                    'sandbox.network="enabled"',
                    f"sandbox.windows_state_dir={json.dumps(str(root / 'state'))}",
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
                "version": 2,
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
            self.assertIn(b"mcp-console sandbox-setup", result.stderr)
            self.assertFalse((root / "state").exists())

    def test_modified_helper_is_rejected_before_setup(self):
        with workspace() as root:
            source = BINARY.resolve().parent.parent
            (root / "bin").mkdir()
            shutil.copy2(BINARY, root / "bin/mcp-console.exe")
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

    def test_python_state_and_restart_with_private_temporary_storage(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.windows_sandbox_level="restricted-token"',
                    'sandbox.network="enabled"',
                    f"sandbox.windows_state_dir={json.dumps(str(root / 'state'))}",
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

    def test_restricted_network_does_not_fall_back_to_restricted_token(self):
        with workspace() as root:
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "-c",
                    'sandbox.windows_sandbox_level="restricted-token"',
                    "-c",
                    f"sandbox.windows_state_dir={json.dumps(str(root / 'state'))}",
                    "--",
                    "cmd.exe",
                    "/d",
                    "/c",
                    "echo launched",
                ],
                cwd=root,
                env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"restricted networking requires the elevated", result.stderr)
            self.assertFalse((root / "state").exists())

    def test_stdio_write_policy_and_exit_status(self):
        with workspace() as root:
            work = root / "work"
            work.mkdir()
            (work / "input.txt").write_text("readable", encoding="utf-8")
            config = {
                "version": 2,
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
                "windows_sandbox_level": "restricted-token",
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


if __name__ == "__main__":
    unittest.main()
