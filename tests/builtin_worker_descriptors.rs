#![cfg(target_os = "macos")]

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

#[test]
fn builtin_worker_closes_unlisted_server_descriptors() {
    let directory = TestDirectory::new("builtin-worker-descriptors");
    let host_path = directory.path().join("host.txt");
    let host_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&host_path)
        .expect("host descriptor fixture should open");
    let descriptor = duplicate_inheritable(&host_file, 64);

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .current_dir(directory.path())
        .env(
            "MCP_CONSOLE_TEST_INHERITED_FD",
            descriptor.as_raw_fd().to_string(),
        );

    let mut client = McpClient::spawn(command);
    let output = client.call_python(
        2,
        r#"import errno
import os

descriptor = int(os.environ["MCP_CONSOLE_TEST_INHERITED_FD"])
try:
    os.write(descriptor, b"escaped")
except OSError as error:
    assert error.errno == errno.EBADF
else:
    raise RuntimeError("unlisted server descriptor reached the worker")

print("closed")
"#,
    );
    assert_eq!(output, "closed\n");
    drop(client);
    drop(descriptor);
    drop(host_file);

    assert_eq!(
        fs::read(&host_path).expect("host descriptor fixture should be readable"),
        b""
    );
}

fn duplicate_inheritable(file: &File, minimum: libc::c_int) -> OwnedFd {
    let descriptor = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD, minimum) };
    assert!(
        descriptor >= minimum,
        "failed to duplicate descriptor: {}",
        std::io::Error::last_os_error()
    );
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert!(
        flags >= 0,
        "failed to read descriptor flags: {}",
        std::io::Error::last_os_error()
    );
    assert_eq!(
        unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags & !libc::FD_CLOEXEC) },
        0,
        "failed to make descriptor inheritable: {}",
        std::io::Error::last_os_error()
    );
    unsafe { OwnedFd::from_raw_fd(descriptor) }
}

struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new(name: &str) -> Self {
        static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after the Unix epoch")
            .as_nanos();
        let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/sandbox-tests")
            .join(format!(
                "{name}-{}-{unique}-{sequence}",
                std::process::id()
            ));
        fs::create_dir_all(&path).expect("test directory should be created");
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("test directory should be removed");
    }
}

struct McpClient {
    server: Child,
    input: Option<std::process::ChildStdin>,
    output: BufReader<std::process::ChildStdout>,
    closed: bool,
}

impl McpClient {
    fn spawn(mut command: Command) -> Self {
        let mut server = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("mcp-console should start");
        let input = server.stdin.take().expect("stdin should be piped");
        let output = BufReader::new(server.stdout.take().expect("stdout should be piped"));
        let mut client = Self {
            server,
            input: Some(input),
            output,
            closed: false,
        };
        let initialize = client.request(
            1,
            "initialize",
            Some(json!({
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "acceptance-test", "version": "1.0.0"}
            })),
        );
        assert_eq!(initialize["result"]["protocolVersion"], "2025-11-25");
        write_message(
            client.input.as_mut().expect("stdin should be open"),
            &json!({
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }),
        );
        client
    }

    fn call_python(&mut self, id: u64, source: &str) -> String {
        let response = self.request(
            id,
            "tools/call",
            Some(json!({
                "name": "send",
                "arguments": {"python": source}
            })),
        );
        assert_eq!(response["result"]["isError"], false, "{response}");
        assert_eq!(response["result"]["content"][0]["type"], "text");
        response["result"]["content"][0]["text"]
            .as_str()
            .expect("console output should be text")
            .to_owned()
    }

    fn request(&mut self, id: u64, method: &str, params: Option<Value>) -> Value {
        let mut message = json!({"jsonrpc": "2.0", "id": id, "method": method});
        if let Some(params) = params {
            message["params"] = params;
        }
        write_message(self.input.as_mut().expect("stdin should be open"), &message);
        let response = read_message(&mut self.output);
        assert_eq!(response["jsonrpc"], "2.0");
        assert_eq!(response["id"], id);
        response
    }

    fn close(&mut self) {
        if self.closed {
            return;
        }
        drop(self.input.take());
        let deadline = Instant::now() + Duration::from_secs(5);
        let status = loop {
            if let Some(status) = self
                .server
                .try_wait()
                .expect("server status should be readable")
            {
                break status;
            }
            if Instant::now() >= deadline {
                let _ = self.server.kill();
                let _ = self.server.wait();
                panic!("mcp-console did not stop within five seconds");
            }
            std::thread::sleep(Duration::from_millis(10));
        };
        self.closed = true;
        let mut stderr = Vec::new();
        self.server
            .stderr
            .take()
            .expect("stderr should be piped")
            .read_to_end(&mut stderr)
            .expect("stderr should be readable");
        assert!(status.success());
        assert!(
            stderr.is_empty(),
            "server stderr: {}",
            String::from_utf8_lossy(&stderr)
        );
    }
}

impl Drop for McpClient {
    fn drop(&mut self) {
        if self.closed {
            return;
        }
        if std::thread::panicking() {
            drop(self.input.take());
            let _ = self.server.kill();
            let _ = self.server.wait();
            self.closed = true;
        } else {
            self.close();
        }
    }
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
