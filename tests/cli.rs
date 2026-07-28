#[cfg(target_os = "macos")]
use std::fs;
use std::io::{BufRead, BufReader, Write};
#[cfg(target_os = "macos")]
use std::net::TcpListener;
#[cfg(target_os = "macos")]
use std::os::unix::fs::PermissionsExt;
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

#[cfg(target_os = "macos")]
struct TestDirectory(PathBuf);

#[cfg(target_os = "macos")]
impl TestDirectory {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after the Unix epoch")
            .as_nanos();
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/sandbox-tests")
            .join(format!("{name}-{}-{unique}", std::process::id()));
        fs::create_dir_all(&path).expect("test directory should be created");
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

#[cfg(target_os = "macos")]
impl Drop for TestDirectory {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("test directory should be removed");
    }
}

#[test]
fn version_reports_the_binary_name_and_package_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("--version")
        .output()
        .expect("mcp-console should run");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be UTF-8"),
        format!("mcp-console {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert!(output.stderr.is_empty());
}

#[test]
fn stdio_server_registers_only_an_echoing_console_tool() {
    assert_stdio_server(&[]);
    assert_stdio_server(&["serve"]);
}

fn assert_stdio_server(arguments: &[&str]) {
    let mut server = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("mcp-console should start");
    let mut server_input = server.stdin.take().expect("stdin should be piped");
    let mut server_output = BufReader::new(server.stdout.take().expect("stdout should be piped"));

    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "acceptance-test",
                    "version": "1.0.0"
                }
            }
        }),
    );
    let initialize = read_message(&mut server_output);
    assert_eq!(initialize["jsonrpc"], "2.0");
    assert_eq!(initialize["id"], 1);
    assert_eq!(initialize["result"]["protocolVersion"], "2025-11-25");
    assert_eq!(initialize["result"]["capabilities"], json!({"tools": {}}));
    assert_eq!(initialize["result"]["serverInfo"]["name"], "mcp-console");
    assert_eq!(
        initialize["result"]["serverInfo"]["version"],
        env!("CARGO_PKG_VERSION")
    );

    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }),
    );
    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }),
    );
    let tools = read_message(&mut server_output);
    assert_eq!(tools["jsonrpc"], "2.0");
    assert_eq!(tools["id"], 2);
    assert_eq!(
        tools["result"]["tools"]
            .as_array()
            .expect("tools should be an array")
            .len(),
        1
    );
    assert_eq!(tools["result"]["tools"][0]["name"], "console");
    assert_eq!(tools["result"]["tools"][0]["inputSchema"]["type"], "object");

    let arguments = json!({
        "python": "print('hello')",
        "wait_ms": 0
    });
    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "console",
                "arguments": arguments
            }
        }),
    );
    let call = read_message(&mut server_output);
    assert_eq!(call["jsonrpc"], "2.0");
    assert_eq!(call["id"], 3);
    assert_eq!(call["result"]["content"][0]["type"], "text");
    assert_eq!(
        serde_json::from_str::<Value>(
            call["result"]["content"][0]["text"]
                .as_str()
                .expect("console output should be text")
        )
        .expect("console output should be JSON"),
        arguments
    );

    drop(server_input);
    drop(server_output);
    let output = server
        .wait_with_output()
        .expect("mcp-console should stop when stdin closes");
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
}

fn write_message(writer: &mut impl Write, message: &Value) {
    writeln!(writer, "{message}").expect("MCP message should be written");
    writer.flush().expect("MCP message should be flushed");
}

fn read_message(reader: &mut impl BufRead) -> Value {
    let mut line = String::new();
    reader
        .read_line(&mut line)
        .expect("MCP message should be read");
    serde_json::from_str(&line).expect("MCP message should be JSON")
}

#[test]
fn sandbox_requires_a_command() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("sandbox")
        .output()
        .expect("mcp-console should run");

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be UTF-8"),
        "usage: mcp-console sandbox [--] COMMAND [ARG]...\n"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_preserves_python_arguments_and_standard_output() {
    let script = r#"
import sys

print("|".join(sys.argv[1:]))
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args([
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "hello world",
            "$(not-a-command)",
            "--child-option",
        ])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be UTF-8"),
        "hello world|$(not-a-command)|--child-option\n"
    );
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_read_only_policy_allows_python_multiprocessing_semaphores() {
    let script = r#"
import multiprocessing as mp
import operator

context = mp.get_context("spawn")
lock = context.Lock()
lock.acquire()
child = context.Process(target=operator.methodcaller("release"), args=(lock,))
child.start()
child.join()
assert child.exitcode == 0
assert lock.acquire(timeout=1)
print("semaphore shared")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"semaphore shared\n");
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_does_not_require_home() {
    let script = r#"
print("ran")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .env_remove("HOME")
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(output.stdout, b"ran\n");
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_supports_r_runtime_queries_and_temporary_writes() {
    let script = r#"
stopifnot(parallel::detectCores() >= 1)

output <- file.path(tempdir(), "result.txt")
writeLines("sandboxed R", output)
writeLines(readLines(output))
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed R failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed R\n");
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_read_only_policy_allows_processx_pty_processes() {
    let script = r#"
p <- processx::process$new("/bin/cat", pty = TRUE)
on.exit(if (p$is_alive()) p$kill())
p$write_input("sandboxed pty\n")
stopifnot(p$poll_io(5000)[["output"]] == "ready")
cat(p$read_output())
invisible(p$kill())
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed processx failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed pty\r\n");
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_cannot_open_a_preexisting_pseudo_terminal() {
    let host_script = r#"
import os
import pty
import subprocess
import sys

master, slave = pty.openpty()
try:
    result = subprocess.run(
        [sys.argv[1], "sandbox", "--", "python", "-c", sys.argv[2], os.ttyname(slave)],
        capture_output=True,
    )
finally:
    os.close(master)
    os.close(slave)

sys.stdout.buffer.write(result.stdout)
sys.stderr.buffer.write(result.stderr)
raise SystemExit(result.returncode)
"#;
    let sandboxed_script = r#"
import errno
import os
import sys

for flags in (os.O_RDONLY, os.O_WRONLY):
    try:
        descriptor = os.open(sys.argv[1], flags | os.O_NOCTTY)
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        os.close(descriptor)
        raise SystemExit("pre-existing pseudo-terminal was accessible")

print("blocked")
"#;
    let output = Command::new("python")
        .args(["-c", host_script])
        .arg(env!("CARGO_BIN_EXE_mcp-console"))
        .arg(sandboxed_script)
        .output()
        .expect("Python PTY fixture should run");

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"blocked\n");
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_is_read_only_except_for_a_dedicated_temp_directory() {
    let test_directory = TestDirectory::new("write-boundary");
    let workspace = test_directory.path().join("workspace");
    let home = test_directory.path().join("home");
    fs::create_dir(&workspace).expect("workspace should be created");
    fs::create_dir(&home).expect("home directory should be created");
    let workspace_file = workspace.join("workspace.txt");
    let home_file = home.join("home.txt");
    fs::write(&workspace_file, "host data").expect("workspace fixture should be created");
    fs::write(&home_file, "host data").expect("home fixture should be created");
    let shared_tmp_file = Path::new("/tmp").join(format!(
        "{}.txt",
        test_directory
            .path()
            .file_name()
            .expect("test directory should have a name")
            .to_string_lossy()
    ));
    let script = r#"
import errno
import pathlib
import sys
import tempfile

temp_dir = pathlib.Path(tempfile.gettempdir())
(temp_dir / "allowed.txt").write_text("temporary")
host_files = list(map(pathlib.Path, sys.argv[1:3]))
assert all(path.read_text() == "host data" for path in host_files)

allowed = []
for path in [*host_files, pathlib.Path(sys.argv[3])]:
    try:
        path.write_text("escaped")
    except OSError as error:
        assert error.errno == errno.EPERM
    else:
        allowed.append(str(path))

if allowed:
    raise SystemExit(f"host writes were allowed: {', '.join(allowed)}")

print(temp_dir)
"#;

    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .current_dir(&workspace)
        .env("HOME", &home)
        .env("TMPDIR", &home)
        .args(["sandbox", "--", "python", "-c", script])
        .arg(&workspace_file)
        .arg(&home_file)
        .arg(&shared_tmp_file)
        .output()
        .expect("mcp-console sandbox should run");

    let shared_tmp_was_written = shared_tmp_file.exists();
    if shared_tmp_was_written {
        fs::remove_file(&shared_tmp_file).expect("shared temp test file should be removed");
    }

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let dedicated_temp = PathBuf::from(
        String::from_utf8(output.stdout)
            .expect("stdout should be UTF-8")
            .trim(),
    );
    assert!(dedicated_temp.starts_with(&home));
    assert_ne!(dedicated_temp, home);
    assert!(
        dedicated_temp
            .file_name()
            .expect("dedicated temp directory should have a name")
            .to_string_lossy()
            .starts_with("mcp-console-tmp-")
    );
    assert_eq!(
        fs::read_to_string(&workspace_file).expect("workspace fixture should remain readable"),
        "host data"
    );
    assert_eq!(
        fs::read_to_string(&home_file).expect("home fixture should remain readable"),
        "host data"
    );
    assert!(!shared_tmp_was_written);
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_cannot_hard_link_host_files_into_its_writable_temp_directory() {
    let temp_root = TestDirectory::new("hard-link-boundary");
    let host_file = temp_root.path().join("host.txt");
    fs::write(&host_file, "host data").expect("host fixture should be created");
    let script = r#"
import errno
import os
import pathlib
import sys

destination = pathlib.Path(os.environ["TMPDIR"]) / "host-link"
assert os.stat(sys.argv[1]).st_dev == os.stat(destination.parent).st_dev
try:
    os.link(sys.argv[1], destination)
except OSError as error:
    assert error.errno == errno.EPERM
else:
    destination.write_text("escaped")
    raise SystemExit("host file was linked into the writable temp directory")

print("blocked")
"#;

    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .env("TMPDIR", temp_root.path())
        .args(["sandbox", "--", "python", "-c", script])
        .arg(&host_file)
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"blocked\n");
    assert_eq!(
        fs::read_to_string(host_file).expect("host fixture should remain readable"),
        "host data"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_denies_network_access() {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("test listener should bind");
    let port = listener
        .local_addr()
        .expect("test listener should have an address")
        .port()
        .to_string();
    let script = r#"
import errno
import socket
import sys

try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])))
except OSError as error:
    assert error.errno == errno.EPERM
    print("blocked")
else:
    raise SystemExit("network access was allowed")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "python", "-c", script])
        .arg(port)
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(output.stdout, b"blocked\n");
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_preserves_child_exit_status_after_temp_permissions_change() {
    let temp_root = TestDirectory::new("locked-temp");
    let script = r#"
import os
import pathlib

temp_dir = pathlib.Path(os.environ["TMPDIR"])
locked = temp_dir / "locked"
locked.mkdir()
(locked / "data.txt").write_text("data")
locked.chmod(0)
raise SystemExit(23)
"#;
    let status = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .env("TMPDIR", temp_root.path())
        .args(["sandbox", "--", "python", "-c", script])
        .status()
        .expect("mcp-console sandbox should run");

    for entry in fs::read_dir(temp_root.path()).expect("temp root should be readable") {
        let temp_directory = entry.expect("temp entry should be readable").path();
        let locked = temp_directory.join("locked");
        if locked.exists() {
            let mut permissions = fs::metadata(&locked)
                .expect("locked directory should be readable")
                .permissions();
            permissions.set_mode(0o700);
            fs::set_permissions(locked, permissions).expect("locked directory should be unlocked");
        }
        fs::remove_dir_all(temp_directory).expect("dedicated temp directory should be removed");
    }

    assert_eq!(status.code(), Some(23));
}

#[cfg(not(target_os = "macos"))]
#[test]
fn sandbox_is_unsupported_on_this_operating_system() {
    let script = r#"
print("not run")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console should run");

    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be UTF-8"),
        "`mcp-console sandbox` is not supported on this operating system\n"
    );
}
