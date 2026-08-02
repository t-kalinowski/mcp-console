#![cfg(target_os = "macos")]

use std::process::Command;

#[test]
fn sandbox_delivers_a_terminal_interrupt_once() {
    let host_script = r#"
import fcntl
import os
import pty
import signal
import subprocess
import sys
import termios

master, slave = pty.openpty()
sandbox_group = None

def attach_controlling_terminal():
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(slave, os.getpid())

process = subprocess.Popen(
    [sys.argv[1], "sandbox", "--", "python", "-c", sys.argv[2]],
    stdin=slave,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    preexec_fn=attach_controlling_terminal,
)
os.close(slave)
try:
    assert process.stdout.readline() == "ready\n"
    sandbox_group = os.tcgetpgrp(master)
    os.write(master, b"sandbox input\n")
    assert process.stdout.readline() == "sandbox input\n"
    os.write(master, b"\x03")
    stdout, stderr = process.communicate(timeout=5)
except BaseException:
    for process_group in (sandbox_group, process.pid):
        if process_group is None:
            continue
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()
    raise
finally:
    os.close(master)

sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(process.returncode)
"#;
    let sandboxed_script = r#"
import signal
import time

interrupts = 0

def handle_interrupt(_signal, _frame):
    global interrupts
    interrupts += 1

signal.signal(signal.SIGINT, handle_interrupt)
print("ready", flush=True)
print(input(), flush=True)
deadline = time.monotonic() + 0.25
while time.monotonic() < deadline:
    time.sleep(0.01)
print(interrupts)
"#;
    let output = Command::new("python")
        .args(["-c", host_script])
        .arg(env!("CARGO_BIN_EXE_mcp-console"))
        .arg(sandboxed_script)
        .output()
        .expect("terminal fixture should run");

    assert!(
        output.status.success(),
        "terminal fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"1\n");
}

#[test]
fn sandbox_preserves_status_after_its_controlling_terminal_closes() {
    let host_script = r#"
import ctypes
import fcntl
import os
import pty
import signal
import subprocess
import sys
import termios

master, slave = pty.openpty()
slave_name = os.ttyname(slave)
sandbox_group = None

def attach_controlling_terminal():
    os.setsid()
    fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    os.tcsetpgrp(slave, os.getpid())

process = subprocess.Popen(
    [sys.argv[1], "sandbox", "--", "python", "-c", sys.argv[2]],
    stdin=slave,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    preexec_fn=attach_controlling_terminal,
)
os.close(slave)
try:
    assert process.stdout.readline() == "ready\n"
    sandbox_group = os.tcgetpgrp(master)
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.revoke(slave_name.encode()) == 0
    os.close(master)
    master = None
    stdout, stderr = process.communicate(timeout=5)
except BaseException:
    for process_group in (sandbox_group, process.pid):
        if process_group is None:
            continue
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()
    raise
finally:
    if master is not None:
        os.close(master)

sys.stdout.write(stdout)
sys.stderr.write(stderr)
raise SystemExit(process.returncode)
"#;
    let sandboxed_script = r#"
import signal
import time

signal.signal(signal.SIGHUP, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(0.1)
raise SystemExit(23)
"#;
    let output = Command::new("python")
        .args(["-c", host_script])
        .arg(env!("CARGO_BIN_EXE_mcp-console"))
        .arg(sandboxed_script)
        .output()
        .expect("terminal fixture should run");

    assert_eq!(
        output.status.code(),
        Some(23),
        "terminal fixture failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
}
