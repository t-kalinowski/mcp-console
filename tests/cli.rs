#[cfg(unix)]
use std::collections::BTreeSet;
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(target_os = "macos")]
use std::net::TcpListener;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(target_os = "macos")]
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
#[cfg(unix)]
use std::sync::mpsc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

struct TestDirectory(PathBuf);

#[cfg(unix)]
const FIXTURE_EVENT_TIMEOUT: Duration = Duration::from_secs(15);
#[cfg(unix)]
const TEST_EVENT_FD_ENV: &str = "MCP_CONSOLE_TEST_EVENT_FD";
#[cfg(unix)]
const TEST_RESPONSE_GATE_FD_ENV: &str = "MCP_CONSOLE_TEST_RESPONSE_GATE_FD";
#[cfg(unix)]
const TEST_RESPONSE_GATE_OPERATION_ENV: &str = "MCP_CONSOLE_TEST_RESPONSE_GATE_OPERATION";
#[cfg(unix)]
const TEST_OPERATION_GATE_FD_ENV: &str = "MCP_CONSOLE_TEST_OPERATION_GATE_FD";
#[cfg(unix)]
const TEST_OPERATION_GATE_OPERATION_ENV: &str = "MCP_CONSOLE_TEST_OPERATION_GATE_OPERATION";
#[cfg(unix)]
const TEST_STDIN_GATE_FD_ENV: &str = "MCP_CONSOLE_TEST_STDIN_GATE_FD";
#[cfg(unix)]
const TEST_STDIN_GATE_OPERATION_ENV: &str = "MCP_CONSOLE_TEST_STDIN_GATE_OPERATION";
#[cfg(unix)]
const TEST_FIXTURE_CLEANUP_FD_ENV: &str = "MCP_CONSOLE_TEST_FIXTURE_CLEANUP_FD";

#[cfg(unix)]
struct FixtureControl {
    events: mpsc::Receiver<Result<Value, String>>,
    trace: Vec<Value>,
    outstanding: BTreeSet<u64>,
    event_writer: Option<std::io::PipeWriter>,
    response_gate_reader: Option<std::io::PipeReader>,
    response_gate_release: Option<std::io::PipeWriter>,
    operation_gate_reader: Option<std::io::PipeReader>,
    operation_gate_release: Option<std::io::PipeWriter>,
    stdin_gate_reader: Option<std::io::PipeReader>,
    stdin_gate_release: Option<std::io::PipeWriter>,
    fixture_cleanup_reader: Option<std::io::PipeReader>,
    fixture_cleanup: Option<std::io::PipeWriter>,
}

#[cfg(target_os = "macos")]
struct KillOnDrop(Option<libc::pid_t>);

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
            .join(format!("{name}-{}-{unique}-{sequence}", std::process::id()));
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

#[cfg(unix)]
impl FixtureControl {
    fn new(command: &mut Command) -> Self {
        let (event_reader, event_writer) = std::io::pipe().expect("event pipe should be created");
        make_inheritable(&event_writer);
        command.env(TEST_EVENT_FD_ENV, event_writer.as_raw_fd().to_string());

        let (sender, events) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(event_reader).lines() {
                let event = line
                    .map_err(|error| format!("fixture event read failed: {error}"))
                    .and_then(|line| {
                        serde_json::from_str(&line)
                            .map_err(|error| format!("invalid fixture event {line:?}: {error}"))
                    });
                if sender.send(event).is_err() {
                    return;
                }
            }
        });

        Self {
            events,
            trace: Vec::new(),
            outstanding: BTreeSet::new(),
            event_writer: Some(event_writer),
            response_gate_reader: None,
            response_gate_release: None,
            operation_gate_reader: None,
            operation_gate_release: None,
            stdin_gate_reader: None,
            stdin_gate_release: None,
            fixture_cleanup_reader: None,
            fixture_cleanup: None,
        }
    }

    fn gate_response(&mut self, command: &mut Command, operation: u64) {
        let (reader, writer) = std::io::pipe().expect("response gate pipe should be created");
        make_inheritable(&reader);
        command
            .env(TEST_RESPONSE_GATE_FD_ENV, reader.as_raw_fd().to_string())
            .env(TEST_RESPONSE_GATE_OPERATION_ENV, operation.to_string());
        self.response_gate_reader = Some(reader);
        self.response_gate_release = Some(writer);
    }

    fn gate_operation(&mut self, command: &mut Command, operation: u64) {
        let (reader, writer) = std::io::pipe().expect("operation gate pipe should be created");
        make_inheritable(&reader);
        command
            .env(TEST_OPERATION_GATE_FD_ENV, reader.as_raw_fd().to_string())
            .env(TEST_OPERATION_GATE_OPERATION_ENV, operation.to_string());
        self.operation_gate_reader = Some(reader);
        self.operation_gate_release = Some(writer);
    }

    fn gate_stdin(&mut self, command: &mut Command, operation: u64) {
        let (reader, writer) = std::io::pipe().expect("stdin gate pipe should be created");
        make_inheritable(&reader);
        command
            .env(TEST_STDIN_GATE_FD_ENV, reader.as_raw_fd().to_string())
            .env(TEST_STDIN_GATE_OPERATION_ENV, operation.to_string());
        self.stdin_gate_reader = Some(reader);
        self.stdin_gate_release = Some(writer);
    }

    fn control_fixture_cleanup(&mut self, command: &mut Command) {
        let (reader, writer) = std::io::pipe().expect("cleanup pipe should be created");
        make_inheritable(&reader);
        command.env(TEST_FIXTURE_CLEANUP_FD_ENV, reader.as_raw_fd().to_string());
        self.fixture_cleanup_reader = Some(reader);
        self.fixture_cleanup = Some(writer);
    }

    fn child_spawned(&mut self) {
        drop(self.event_writer.take());
        drop(self.response_gate_reader.take());
        drop(self.operation_gate_reader.take());
        drop(self.stdin_gate_reader.take());
        drop(self.fixture_cleanup_reader.take());
    }

    fn release_response(&mut self) {
        release_pipe(&mut self.response_gate_release, "response gate");
    }

    fn release_operation(&mut self) {
        release_pipe(&mut self.operation_gate_release, "operation gate");
    }

    fn cleanup_fixture(&mut self) {
        release_pipe(&mut self.fixture_cleanup, "fixture cleanup");
    }

    fn wait_for(&mut self, operation: u64, kind: &str) -> Value {
        if let Some(event) = self.find(operation, kind) {
            return event;
        }
        let started = Instant::now();
        loop {
            let remaining = FIXTURE_EVENT_TIMEOUT.saturating_sub(started.elapsed());
            if remaining.is_zero() {
                self.fail(format!(
                    "timed out waiting for operation {operation} event {kind:?}"
                ));
            }
            match self.events.recv_timeout(remaining) {
                Ok(Ok(event)) => {
                    self.record(event);
                    if let Some(event) = self.find(operation, kind) {
                        return event;
                    }
                }
                Ok(Err(error)) => self.fail(error),
                Err(mpsc::RecvTimeoutError::Timeout) => self.fail(format!(
                    "timed out waiting for operation {operation} event {kind:?}"
                )),
                Err(mpsc::RecvTimeoutError::Disconnected) => self.fail(format!(
                    "event stream closed while waiting for operation {operation} event {kind:?}"
                )),
            }
        }
    }

    fn assert_absent(&mut self, operation: u64, kind: &str) {
        let diagnostics = self.diagnostics();
        let absent = self.find(operation, kind).is_none();
        assert!(
            absent,
            "unexpected operation {operation} event {kind:?};{diagnostics}"
        );
    }

    fn assert_before(&mut self, first: (u64, &str), second: (u64, &str)) {
        self.drain();
        let first_index = self.event_index(first.0, first.1).unwrap_or_else(|| {
            self.fail(format!("missing operation {} event {:?}", first.0, first.1))
        });
        let second_index = self.event_index(second.0, second.1).unwrap_or_else(|| {
            self.fail(format!(
                "missing operation {} event {:?}",
                second.0, second.1
            ))
        });
        let diagnostics = self.diagnostics();
        assert!(
            first_index < second_index,
            "operation {} event {:?} followed operation {} event {:?};{}",
            first.0,
            first.1,
            second.0,
            second.1,
            diagnostics
        );
    }

    fn assert_response_id(&mut self, response: &Value, expected: u64) {
        let diagnostics = self.diagnostics();
        assert_eq!(
            response["id"], expected,
            "unexpected response: {response};{diagnostics}"
        );
    }

    fn drain(&mut self) {
        loop {
            match self.events.try_recv() {
                Ok(Ok(event)) => self.record(event),
                Ok(Err(error)) => self.fail(error),
                Err(mpsc::TryRecvError::Empty | mpsc::TryRecvError::Disconnected) => return,
            }
        }
    }

    fn record(&mut self, event: Value) {
        if let Some(operation) = event["operation"].as_u64() {
            match event["kind"].as_str() {
                Some("operation_accepted") => {
                    self.outstanding.insert(operation);
                }
                Some("cancellation_before_admission" | "response_write_completed") => {
                    self.outstanding.remove(&operation);
                }
                Some("operation_stopped")
                    if self
                        .find(operation, "cancellation_after_admission")
                        .is_some() =>
                {
                    self.outstanding.remove(&operation);
                }
                _ => {}
            }
        }
        self.trace.push(event);
    }

    fn find(&self, operation: u64, kind: &str) -> Option<Value> {
        self.trace
            .iter()
            .find(|event| event["operation"] == operation && event["kind"] == kind)
            .cloned()
    }

    fn event_index(&self, operation: u64, kind: &str) -> Option<usize> {
        self.trace
            .iter()
            .position(|event| event["operation"] == operation && event["kind"] == kind)
    }

    fn formatted_trace(&self) -> String {
        serde_json::to_string_pretty(&self.trace).expect("fixture trace should serialize")
    }

    fn diagnostics(&mut self) -> String {
        self.drain();
        format!(
            " outstanding operations: {:?}; trace:\n{}",
            self.outstanding,
            self.formatted_trace()
        )
    }

    fn fail(&self, message: String) -> ! {
        panic!(
            "{message}; outstanding operations: {:?}; trace:\n{}",
            self.outstanding,
            self.formatted_trace()
        )
    }
}

#[cfg(unix)]
fn make_inheritable(descriptor: &impl AsRawFd) {
    let descriptor = descriptor.as_raw_fd();
    // SAFETY: `descriptor` is open and remains owned by its Rust pipe wrapper.
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert!(flags >= 0, "pipe descriptor flags should be readable");
    // SAFETY: this changes only the close-on-exec flag of the open descriptor.
    assert_eq!(
        unsafe { libc::fcntl(descriptor, libc::F_SETFD, flags & !libc::FD_CLOEXEC) },
        0,
        "pipe descriptor should be inheritable"
    );
}

#[cfg(unix)]
fn release_pipe(pipe: &mut Option<std::io::PipeWriter>, description: &str) {
    let mut pipe = pipe
        .take()
        .unwrap_or_else(|| panic!("{description} should be pending"));
    pipe.write_all(b"1")
        .unwrap_or_else(|error| panic!("{description} release should succeed: {error}"));
}

#[cfg(target_os = "macos")]
fn wait_for_process_event(path: &Path, operation: u64, kind: &str) -> Value {
    let started = Instant::now();
    loop {
        match fs::read_to_string(path) {
            Ok(contents) => {
                let event: Value = serde_json::from_str(&contents).unwrap_or_else(|error| {
                    panic!(
                        "invalid process event at {}: {error}; contents: {contents:?}",
                        path.display()
                    )
                });
                assert_eq!(event["operation"], operation, "{event}");
                assert_eq!(event["kind"], kind, "{event}");
                return event;
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => panic!("failed to read process event {}: {error}", path.display()),
        }
        assert!(
            started.elapsed() < FIXTURE_EVENT_TIMEOUT,
            "timed out waiting for operation {operation} event {kind:?} at {}",
            path.display()
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(target_os = "macos")]
fn process_event_pid(event: &Value) -> libc::pid_t {
    event["pid"]
        .as_i64()
        .and_then(|process| libc::pid_t::try_from(process).ok())
        .filter(|process| *process > 0)
        .expect("process event PID should be positive")
}

#[cfg(target_os = "macos")]
impl KillOnDrop {
    fn new(process: libc::pid_t) -> Self {
        Self(Some(process))
    }

    fn disarm(&mut self) {
        self.0 = None;
    }
}

#[cfg(target_os = "macos")]
impl Drop for KillOnDrop {
    fn drop(&mut self) {
        let Some(process) = self.0 else {
            return;
        };
        // SAFETY: the fixture records the child PID immediately after `fork`, and
        // this guard lives only for that test invocation.
        let _ = unsafe { libc::kill(process, libc::SIGKILL) };
    }
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_accepts_long_multibyte_source_lines() {
    let mut client = McpClient::start(&["serve"]);
    let long_value = "é".repeat(100_000);
    let long_line = format!(
        r#"
long_line_value <- "{long_value}"
nchar(long_line_value)
"#
    );
    assert_eq!(
        client.call_console(2, json!({"r": long_line})),
        "[1] 100000\n"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_orders_requests_and_cancellation_during_response_writes() {
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve");
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    let invalid_send = json!({"r": "1", "python": "1"});
    client.send_tool(2, "send", invalid_send.clone());
    control.wait_for(2, "response_write_paused");

    client.send_tool(3, "send", invalid_send.clone());
    control.wait_for(3, "operation_waiting");
    client.cancel(3);
    control.wait_for(3, "cancellation_before_admission");

    client.send_tool(4, "send", invalid_send);
    control.wait_for(4, "operation_waiting");
    control.release_response();
    control.wait_for(2, "response_write_completed");
    control.wait_for(4, "operation_admitted");
    control.wait_for(4, "response_write_completed");
    control.assert_before((2, "response_write_completed"), (4, "operation_admitted"));
    control.assert_absent(3, "operation_admitted");

    let first = read_message(&mut client.output);
    control.assert_response_id(&first, 2);
    assert_eq!(
        response_text(&first),
        "only one of `r`, `python`, or `sql` may be supplied"
    );
    let second = read_message(&mut client.output);
    control.assert_response_id(&second, 4);
    assert_eq!(
        response_text(&second),
        "only one of `r`, `python`, or `sql` may be supplied"
    );
    write_message(
        client.input.as_mut().expect("stdin should be open"),
        &json!({"jsonrpc": "2.0", "id": 5, "method": "ping"}),
    );
    let ping = read_message(&mut client.output);
    control.assert_response_id(&ping, 5);
    client.close_controlled(&mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_reports_only_successful_interrupt_delivery() {
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve");
    let mut control = FixtureControl::new(&mut command);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    client.send_tool(2, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(2, "response_write_completed");
    control.assert_absent(2, "interrupt_delivered");

    let response = read_message(&mut client.output);
    control.assert_response_id(&response, 2);
    assert_eq!(response["result"]["isError"], true, "{response}");
    client.close_controlled(&mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_releases_calls_received_after_response_gate_settles() {
    let zod = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/zod");
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve").arg("--worker").arg(zod);
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    client.send_console(2, json!({"r": "checkpoint 2"}));
    control.wait_for(2, "worker_operation_started");
    control.wait_for(2, "worker_operation_completed");
    control.wait_for(2, "response_write_paused");

    client.send_tool(3, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(3, "operation_waiting");
    client.cancel(3);
    control.wait_for(3, "cancellation_before_admission");
    client.send_tool(4, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(4, "operation_waiting");
    control.release_response();
    control.wait_for(2, "response_write_completed");
    control.wait_for(4, "operation_admitted");
    control.wait_for(4, "interrupt_delivered");
    control.wait_for(4, "response_write_completed");
    control.assert_before((2, "response_write_completed"), (4, "operation_admitted"));
    control.assert_absent(3, "operation_admitted");
    control.assert_absent(3, "interrupt_delivered");

    let checkpoint = read_message(&mut client.output);
    control.assert_response_id(&checkpoint, 2);
    let interrupt = read_message(&mut client.output);
    control.assert_response_id(&interrupt, 4);
    assert_eq!(response_text(&interrupt), "\n[idle]");
    write_message(
        client.input.as_mut().expect("stdin should be open"),
        &json!({"jsonrpc": "2.0", "id": 5, "method": "ping"}),
    );
    let ping = read_message(&mut client.output);
    control.assert_response_id(&ping, 5);
    client.close_controlled(&mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_keeps_later_calls_behind_an_existing_gated_cohort() {
    let zod = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/zod");
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve").arg("--worker").arg(zod);
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    control.gate_operation(&mut command, 3);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    client.send_console(2, json!({"r": "checkpoint 2"}));
    control.wait_for(2, "response_write_paused");
    client.send_console(3, json!({"r": "complete after test release: 3"}));
    client.send_tool(4, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(3, "operation_waiting");
    control.wait_for(4, "operation_waiting");

    control.release_response();
    control.wait_for(2, "response_write_completed");
    control.wait_for(3, "operation_admitted");
    control.wait_for(3, "worker_operation_started");

    client.send_tool(5, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(5, "operation_waiting");
    control.release_operation();
    control.wait_for(3, "worker_operation_completed");
    control.wait_for(3, "response_write_completed");
    control.wait_for(4, "operation_admitted");
    control.wait_for(4, "interrupt_delivered");
    control.wait_for(4, "response_write_completed");
    control.wait_for(5, "operation_admitted");
    control.wait_for(5, "interrupt_delivered");
    control.wait_for(5, "response_write_completed");
    control.assert_before((3, "response_write_completed"), (4, "operation_admitted"));
    control.assert_before((4, "response_write_completed"), (5, "operation_admitted"));

    for id in 2..=5 {
        let response = read_message(&mut client.output);
        control.assert_response_id(&response, id);
        if id >= 4 {
            assert_eq!(response_text(&response), "\n[idle]");
        }
    }
    client.close_controlled(&mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_keeps_cancelled_gated_operation_ordered_until_it_stops() {
    let zod = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/zod");
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve").arg("--worker").arg(zod);
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    control.gate_operation(&mut command, 3);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    client.send_console(2, json!({"r": "checkpoint 2"}));
    control.wait_for(2, "response_write_paused");
    client.send_console(3, json!({"r": "complete after test release: 3"}));
    control.wait_for(3, "operation_waiting");
    control.release_response();
    control.wait_for(2, "response_write_completed");
    control.wait_for(3, "operation_admitted");
    control.wait_for(3, "worker_operation_started");
    let checkpoint = read_message(&mut client.output);
    control.assert_response_id(&checkpoint, 2);

    client.cancel(3);
    control.wait_for(3, "cancellation_after_admission");
    write_message(
        client.input.as_mut().expect("stdin should be open"),
        &json!({"jsonrpc": "2.0", "id": 4, "method": "ping"}),
    );
    let ping = read_message(&mut client.output);
    control.assert_response_id(&ping, 4);
    client.send_tool(5, "send", json!({"control": "interrupt", "timeout_ms": 0}));
    control.wait_for(5, "operation_waiting");
    control.release_operation();
    control.wait_for(3, "worker_operation_completed");
    control.wait_for(3, "operation_stopped");
    control.wait_for(5, "operation_admitted");
    control.wait_for(5, "interrupt_delivered");
    control.wait_for(5, "response_write_completed");
    control.assert_before((3, "worker_operation_completed"), (5, "operation_admitted"));
    control.assert_before((3, "operation_stopped"), (5, "operation_admitted"));
    control.assert_absent(3, "response_write_completed");

    let interrupt = read_message(&mut client.output);
    control.assert_response_id(&interrupt, 5);
    assert_eq!(response_text(&interrupt), "[done]");
    client.close_controlled(&mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_bounds_response_gated_calls() {
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve");
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    let invalid_send = json!({"r": "1", "python": "1"});
    client.send_tool(2, "send", invalid_send.clone());
    control.wait_for(2, "response_write_paused");
    for id in 3..=67 {
        client.send_tool(id, "send", invalid_send.clone());
    }
    control.wait_for(67, "transport_closed");
    control.assert_before((66, "operation_accepted"), (67, "transport_closed"));

    client.wait_for_exit_within_controlled(Duration::from_secs(2), &mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_observes_eof_behind_response_gate() {
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve");
    let mut control = FixtureControl::new(&mut command);
    control.gate_response(&mut command, 2);
    let mut client = McpClient::spawn(command);
    control.child_spawned();

    let invalid_send = json!({"r": "1", "python": "1"});
    client.send_tool(2, "send", invalid_send.clone());
    control.wait_for(2, "response_write_paused");
    client.send_tool(3, "send", invalid_send);
    control.wait_for(3, "operation_waiting");

    drop(client.input.take());
    control.wait_for(0, "transport_closed");
    client.wait_for_exit_within_controlled(Duration::from_secs(2), &mut control);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_discovers_r_inside_the_worker_sandbox() {
    let test_directory = TestDirectory::new("native-worker-r-discovery");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    let escaped = test_directory.path().join("escaped.txt");
    let preflight_complete = test_directory.path().join("preflight-complete");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
if [ ! -e "$MCP_CONSOLE_PREFLIGHT_COMPLETE" ]; then
  : > "$MCP_CONSOLE_PREFLIGHT_COMPLETE"
  printf '%s\n' "$MCP_CONSOLE_REAL_R_HOME"
  exit 0
fi
printf escaped > "$MCP_CONSOLE_ESCAPE_PATH"
printf '%s\n' "$MCP_CONSOLE_REAL_R_HOME"
"#,
    )
    .expect("fake R should be written");
    fs::set_permissions(&fake_r, fs::Permissions::from_mode(0o755))
        .expect("fake R should be executable");

    let r_home_output = Command::new("R")
        .arg("RHOME")
        .output()
        .expect("test R should be discoverable");
    assert!(r_home_output.status.success());
    let r_home =
        String::from_utf8(r_home_output.stdout).expect("test R home should be valid UTF-8");
    let path = std::env::join_paths(std::iter::once(fake_bin.clone()).chain(
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
    ))
    .expect("test PATH should be valid");

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env_remove("R_HOME")
        .env("PATH", path)
        .env("MCP_CONSOLE_ESCAPE_PATH", &escaped)
        .env("MCP_CONSOLE_PREFLIGHT_COMPLETE", &preflight_complete)
        .env("MCP_CONSOLE_REAL_R_HOME", r_home.trim());
    let mut client = McpClient::spawn(command);

    assert_eq!(client.call_console(2, json!({"r": "1 + 1"})), "[1] 2\n");
    assert!(
        !escaped.exists(),
        "R discovery must not write outside the worker sandbox"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_uses_worker_r_installation_for_r_preparation() {
    match Command::new("ir")
        .arg("--version")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
    {
        Ok(status) => assert!(status.success(), "ir --version failed with {status}"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            eprintln!("skipping R preparation selection test: ir is not on PATH");
            return;
        }
        Err(error) => panic!("failed to check ir: {error}"),
    }

    let r_home_output = Command::new("R")
        .arg("RHOME")
        .env_remove("R_HOME")
        .output()
        .expect("test R should be discoverable");
    assert!(r_home_output.status.success());
    let real_r_home = String::from_utf8(r_home_output.stdout)
        .expect("test R home should be valid UTF-8")
        .trim()
        .to_string();
    let real_r = Path::new(&real_r_home).join("bin/R");
    assert!(
        real_r.is_file(),
        "test R should exist at {}",
        real_r.display()
    );

    let test_directory = TestDirectory::new("r-preparation-r-installation");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    let fake_rscript = fake_bin.join("Rscript");
    let wrong_rscript_used = test_directory.path().join("wrong-rscript-used");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
exec "$MCP_CONSOLE_REAL_R" "$@"
"#,
    )
    .expect("fake R should be written");
    fs::write(
        &fake_rscript,
        r#"#!/bin/sh
printf used > "$MCP_CONSOLE_WRONG_RSCRIPT_USED"
exit 99
"#,
    )
    .expect("fake Rscript should be written");
    fs::set_permissions(&fake_r, fs::Permissions::from_mode(0o755))
        .expect("fake R should be executable");
    fs::set_permissions(&fake_rscript, fs::Permissions::from_mode(0o755))
        .expect("fake Rscript should be executable");
    let path = std::env::join_paths(std::iter::once(fake_bin).chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    )))
    .expect("test PATH should be valid");

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env_remove("R_HOME")
        // This test isolates R selection from managed-Python startup, whose
        // resolver intentionally uses the configured PATH.
        .env("RETICULATE_PYTHON", "/usr/bin/python3")
        .env("PATH", path)
        .env("MCP_CONSOLE_REAL_R", &real_r)
        .env("MCP_CONSOLE_REAL_R_HOME", &real_r_home)
        .env("MCP_CONSOLE_WRONG_RSCRIPT_USED", &wrong_rscript_used);
    let mut client = McpClient::spawn(command);
    let response = client.request(
        2,
        "tools/call",
        Some(json!({
            "name": "send",
            "arguments": {
                "requirements": {"r": ["praise"]}
            }
        })),
    );
    assert_eq!(response["result"]["isError"], false, "{response}");
    assert_eq!(response_text(&response), "[prepared]");

    assert_eq!(
        client.call_console(
            3,
            json!({"r": r#"
stopifnot(
  normalizePath(R.home()) ==
    normalizePath(Sys.getenv("MCP_CONSOLE_REAL_R_HOME")),
  identical(dirname(find.package("praise")), .libPaths()[[1L]])
)
praise::praise("ready")
"#})
        ),
        "[1] \"ready\"\n"
    );
    assert!(
        !wrong_rscript_used.exists(),
        "R preparation used the unrelated Rscript on PATH"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_during_r_preparation_discovery() {
    let test_directory = TestDirectory::new("r-preparation-discovery-shutdown");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    let resolver_started = test_directory.path().join("resolver-started.json");
    let preflight_complete = test_directory.path().join("preflight-complete");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
if [ ! -e "$MCP_CONSOLE_PREFLIGHT_COMPLETE" ]; then
  : > "$MCP_CONSOLE_PREFLIGHT_COMPLETE"
  printf '%s\n' "$MCP_CONSOLE_REAL_R_HOME"
  exit 0
fi
printf '{"operation":2,"kind":"r_preparation_discovery_started","component":"fixture","pid":%s}\n' "$$" > "${MCP_CONSOLE_PROCESS_EVENT}.tmp"
/bin/mv "${MCP_CONSOLE_PROCESS_EVENT}.tmp" "$MCP_CONSOLE_PROCESS_EVENT"
exec /bin/sleep 4
"#,
    )
    .expect("fake R should be written");
    fs::set_permissions(&fake_r, fs::Permissions::from_mode(0o755))
        .expect("fake R should be executable");
    let path = std::env::join_paths(std::iter::once(fake_bin).chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    )))
    .expect("test PATH should be valid");
    let real_r_home = Command::new("R")
        .arg("RHOME")
        .output()
        .expect("test R should be discoverable");
    assert!(real_r_home.status.success());
    let real_r_home = String::from_utf8(real_r_home.stdout)
        .expect("test R home should be valid UTF-8")
        .trim()
        .to_string();

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env_remove("R_HOME")
        .env("RETICULATE_PYTHON", "")
        .env("PATH", path)
        .env("MCP_CONSOLE_PREFLIGHT_COMPLETE", &preflight_complete)
        .env("MCP_CONSOLE_REAL_R_HOME", &real_r_home)
        .env("MCP_CONSOLE_PROCESS_EVENT", &resolver_started);
    let mut client = McpClient::spawn(command);
    client.send_tool(
        2,
        "send",
        json!({
            "requirements": {"r": ["praise"]}
        }),
    );

    let resolver_group = process_event_pid(&wait_for_process_event(
        &resolver_started,
        2,
        "r_preparation_discovery_started",
    ));

    let elapsed = client.close_within(Duration::from_secs(6));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
    let group_stopped = unsafe { libc::killpg(resolver_group, 0) } < 0
        && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH);
    if !group_stopped {
        // SAFETY: the fake resolver recorded its dedicated process-group ID.
        let _ = unsafe { libc::killpg(resolver_group, libc::SIGKILL) };
    }
    assert!(
        group_stopped,
        "worker R discovery process group outlived server shutdown"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_during_r_discovery() {
    let test_directory = TestDirectory::new("native-worker-r-discovery-shutdown");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    let preflight_complete = test_directory.path().join("preflight-complete");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
if [ ! -e "$MCP_CONSOLE_PREFLIGHT_COMPLETE" ]; then
  : > "$MCP_CONSOLE_PREFLIGHT_COMPLETE"
  printf '%s\n' "$MCP_CONSOLE_REAL_R_HOME"
  exit 0
fi
case "$MCP_CONSOLE_TEST_EVENT_FD" in
  ''|*[!0-9]*) exit 97 ;;
esac
event=$(printf '{"operation":2,"kind":"r_discovery_started","component":"fixture","pid":%s}' "$$")
eval "printf '%s\\n' \"\$event\" >&$MCP_CONSOLE_TEST_EVENT_FD"
exec /bin/sleep 3
"#,
    )
    .expect("fake R should be written");
    fs::set_permissions(&fake_r, fs::Permissions::from_mode(0o755))
        .expect("fake R should be executable");
    let path = std::env::join_paths(std::iter::once(fake_bin).chain(std::env::split_paths(
        &std::env::var_os("PATH").unwrap_or_default(),
    )))
    .expect("test PATH should be valid");
    let real_r_home = Command::new("R")
        .arg("RHOME")
        .output()
        .expect("test R should be discoverable");
    assert!(real_r_home.status.success());
    let real_r_home = String::from_utf8(real_r_home.stdout)
        .expect("test R home should be valid UTF-8")
        .trim()
        .to_string();

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env_remove("R_HOME")
        .env("PATH", path)
        .env("MCP_CONSOLE_PREFLIGHT_COMPLETE", &preflight_complete)
        .env("MCP_CONSOLE_REAL_R_HOME", &real_r_home);
    let mut control = FixtureControl::new(&mut command);
    let mut client = McpClient::spawn(command);
    control.child_spawned();
    client.send_console(2, json!({"r": "1 + 1"}));
    control.wait_for(2, "r_discovery_started");

    let elapsed = client.close_within_controlled(Duration::from_secs(2), &mut control);
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_stops_resolver_descendants_when_leader_exits() {
    let test_directory = TestDirectory::new("python-resolver-descendant-cleanup");
    let fake_bin = test_directory.path().join("bin");
    let fake_ir = fake_bin.join("ir");
    let fake_rscript = fake_bin.join("Rscript");
    let fake_python = test_directory.path().join("python");
    let python_preflight_complete = test_directory.path().join("python-preflight-complete");
    let resolver_started = test_directory.path().join("resolver-started.json");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_ir,
        r#"#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf 'ir 0.4.0\n'
else
  printf '%s' "$MCP_CONSOLE_FAKE_R_LIBRARY"
fi
"#,
    )
    .expect("fake IR should be written");
    fs::write(&fake_python, "").expect("fake Python should be written");
    fs::write(
        &fake_rscript,
        r#"#!/bin/sh
input=$(/bin/cat)
case "$input" in
  *'"extensions"'*)
    exit 0
    ;;
  *'"packages"'*)
    ;;
  *)
    exit 99
    ;;
esac
if [ ! -e "$MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE" ]; then
  : > "$MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE"
  printf '%s\n' "$MCP_CONSOLE_TEST_PYTHON"
  exit 0
fi
/bin/sleep 30 &
printf '{"operation":2,"kind":"python_resolver_started","component":"fixture","pid":%s}\n' "$$" > "${MCP_CONSOLE_PROCESS_EVENT}.tmp"
/bin/mv "${MCP_CONSOLE_PROCESS_EVENT}.tmp" "$MCP_CONSOLE_PROCESS_EVENT"
printf '%s\n' "$MCP_CONSOLE_TEST_PYTHON"
exit 0
"#,
    )
    .expect("fake Rscript should be written");
    fs::set_permissions(&fake_rscript, fs::Permissions::from_mode(0o755))
        .expect("fake Rscript should be executable");
    fs::set_permissions(&fake_ir, fs::Permissions::from_mode(0o755))
        .expect("fake IR should be executable");
    let path = std::env::join_paths(std::iter::once(fake_bin.clone()).chain(
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
    ))
    .expect("test PATH should be valid");

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env("R_HOME", test_directory.path())
        .env("PATH", path)
        .env("RETICULATE_PYTHON", "")
        .env("MCP_CONSOLE_FAKE_R_LIBRARY", test_directory.path())
        .env(
            "MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE",
            &python_preflight_complete,
        )
        .env("MCP_CONSOLE_PROCESS_EVENT", &resolver_started)
        .env("MCP_CONSOLE_TEST_PYTHON", &fake_python);
    let mut client = McpClient::spawn(command);
    client.send_tool(
        2,
        "send",
        json!({
            "requirements": {"python": ["py-yaml12"]}
        }),
    );

    let resolver_group = process_event_pid(&wait_for_process_event(
        &resolver_started,
        2,
        "python_resolver_started",
    ));
    let stopped = Instant::now();
    let group_stopped = loop {
        if unsafe { libc::killpg(resolver_group, 0) } < 0
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        {
            break true;
        }
        if stopped.elapsed() >= Duration::from_secs(2) {
            break false;
        }
        std::thread::sleep(Duration::from_millis(10));
    };
    if !group_stopped {
        // SAFETY: the fake resolver recorded its dedicated process-group ID.
        let _ = unsafe { libc::killpg(resolver_group, libc::SIGKILL) };
    }
    assert!(
        group_stopped,
        "resolver process group outlived its direct process"
    );

    let response = read_message(&mut client.output);
    assert_eq!(response["id"], 2);
    assert_eq!(response["result"]["isError"], false, "{response}");
    assert_eq!(response_text(&response), "[prepared]");
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_during_python_preparation() {
    let test_directory = TestDirectory::new("python-preparation-shutdown");
    let fake_bin = test_directory.path().join("bin");
    let fake_ir = fake_bin.join("ir");
    let fake_rscript = fake_bin.join("Rscript");
    let fake_python = test_directory.path().join("python");
    let python_preflight_complete = test_directory.path().join("python-preflight-complete");
    let resolver_event_base = test_directory.path().join("resolver-started");
    let first_resolver_started = test_directory.path().join("resolver-started-2.json");
    let queued_resolver_started = test_directory.path().join("resolver-started-3.json");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_ir,
        r#"#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
  printf 'ir 0.4.0\n'
else
  printf '%s' "$MCP_CONSOLE_FAKE_R_LIBRARY"
fi
"#,
    )
    .expect("fake IR should be written");
    fs::write(&fake_python, "").expect("fake Python should be written");
    fs::write(
        &fake_rscript,
        r#"#!/bin/sh
input=$(/bin/cat)
case "$input" in
  *'"extensions"'*)
    exit 0
    ;;
  *'"packages"'*)
    ;;
  *)
    exit 99
    ;;
esac
if [ ! -e "$MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE" ]; then
  : > "$MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE"
  printf '%s\n' "$MCP_CONSOLE_TEST_PYTHON"
  exit 0
fi
case "$input" in
  *'py-yaml12'*) operation=2 ;;
  *'scipy'*) operation=3 ;;
  *) exit 98 ;;
esac
event="${MCP_CONSOLE_PROCESS_EVENT_BASE}-${operation}.json"
printf '{"operation":%s,"kind":"python_preparation_started","component":"fixture","pid":%s}\n' "$operation" "$$" > "${event}.tmp"
/bin/mv "${event}.tmp" "$event"
exec /bin/sleep 3
"#,
    )
    .expect("fake Rscript should be written");
    fs::set_permissions(&fake_rscript, fs::Permissions::from_mode(0o755))
        .expect("fake Rscript should be executable");
    fs::set_permissions(&fake_ir, fs::Permissions::from_mode(0o755))
        .expect("fake IR should be executable");
    let path = std::env::join_paths(std::iter::once(fake_bin.clone()).chain(
        std::env::split_paths(&std::env::var_os("PATH").unwrap_or_default()),
    ))
    .expect("test PATH should be valid");

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .env("R_HOME", test_directory.path())
        .env("PATH", path)
        .env_remove("RETICULATE_PYTHON")
        .env("MCP_CONSOLE_FAKE_R_LIBRARY", test_directory.path())
        .env(
            "MCP_CONSOLE_PYTHON_PREFLIGHT_COMPLETE",
            &python_preflight_complete,
        )
        .env("MCP_CONSOLE_PROCESS_EVENT_BASE", &resolver_event_base)
        .env("MCP_CONSOLE_TEST_PYTHON", &fake_python);
    let mut client = McpClient::spawn(command);
    client.send_tool(
        2,
        "send",
        json!({
            "requirements": {"python": ["py-yaml12"]}
        }),
    );

    let resolver_group = process_event_pid(&wait_for_process_event(
        &first_resolver_started,
        2,
        "python_preparation_started",
    ));
    client.send_tool(
        3,
        "send",
        json!({
            "requirements": {"python": ["scipy"]}
        }),
    );
    let tools = client.request(4, "tools/list", None);
    assert_eq!(tools["result"]["tools"].as_array().unwrap().len(), 1);

    let elapsed = client.close_within(Duration::from_secs(2));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
    assert_eq!(unsafe { libc::killpg(resolver_group, 0) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ESRCH),
        "resolver process group outlived server shutdown"
    );
    assert!(
        !queued_resolver_started.exists(),
        "queued preparation started a resolver after shutdown"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_sandboxes_native_r_filesystem_processes_and_network() {
    let test_directory = TestDirectory::new("native-worker-boundary");
    let host_path = test_directory.path().join("host.txt");
    fs::write(&host_path, "host data\n").expect("host fixture should be created");
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("test listener should bind");
    let port = listener
        .local_addr()
        .expect("test listener should have an address")
        .port();
    let host_file = serde_json::to_string(&host_path).expect("host path should serialize");
    let code = format!(
        r#"
temporary_file <- file.path(tempdir(), "allowed.txt")
writeLines("temporary", temporary_file)
host_read <- readLines({host_file})

host_write <- tryCatch({{
    suppressWarnings(writeLines("changed", {host_file}))
    "allowed"
}}, error = function(error) "blocked")

touch_output <- suppressWarnings(system2(
    "/usr/bin/touch",
    {host_file},
    stdout = TRUE,
    stderr = TRUE
))
descendant_write <- if (is.null(attr(touch_output, "status"))) {{
    "allowed"
}} else {{
    "blocked"
}}

network <- tryCatch({{
    connection <- suppressWarnings(socketConnection(
        "127.0.0.1",
        port = {port},
        open = "r+b",
        timeout = 1
    ))
    close(connection)
    "allowed"
}}, error = function(error) "blocked")

cat(
    readLines(temporary_file),
    host_read,
    host_write,
    descendant_write,
    network,
    sep = "|"
)
cat("\n")
"#
    );
    let mut client = McpClient::start(&["serve"]);

    assert_eq!(
        client.call_console(2, json!({"r": code})),
        "temporary|host data|blocked|blocked|blocked\n"
    );
    assert_eq!(
        fs::read_to_string(&host_path).expect("host fixture should remain readable"),
        "host data\n"
    );
    listener
        .set_nonblocking(true)
        .expect("test listener should become nonblocking");
    assert_eq!(
        listener
            .accept()
            .expect_err("sandboxed worker should not reach the listener")
            .kind(),
        std::io::ErrorKind::WouldBlock
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_while_r_waits_for_input() {
    let mut client = McpClient::start(&["serve"]);
    assert_eq!(
        client.call_console(
            2,
            json!({"r": r#"
readline("value> ")
Sys.sleep(60)
"#}),
        ),
        "[input requested: \"value> \"]\n[waiting for stdin]"
    );
    client.send_console(3, json!({"stdin": "resume\n"}));

    let elapsed = client.close_within(Duration::from_secs(2));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_with_background_stderr_descendants() {
    let zod = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/zod");
    let test_directory = TestDirectory::new("background-stderr-shutdown");
    let temporary_path = test_directory.path().to_path_buf();
    let mut environment = std::env::vars().collect::<std::collections::HashMap<_, _>>();
    environment.insert("TMPDIR".to_string(), temporary_path.display().to_string());

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .arg("--worker")
        .arg(zod)
        .envs(environment);
    let mut client = McpClient::spawn(command);
    assert_eq!(
        client.call_console(2, json!({"r": "start background stderr"})),
        "[done]"
    );

    let worker_temporary = fs::read_dir(&temporary_path)
        .expect("test directory should be readable")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .find(|path| path.join("zod-background-stderr-pid").is_file())
        .expect("completed evaluation should have recorded its background descendant");
    let descendant = fs::read_to_string(worker_temporary.join("zod-background-stderr-pid"))
        .expect("background stderr PID should be readable")
        .parse()
        .expect("background stderr PID should be numeric");
    let _descendant = KillOnDrop::new(descendant);

    let elapsed = client.close_within(Duration::from_secs(2));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_shutdown_is_bounded_with_detached_stdin_descendants() {
    let zod = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/zod");
    let test_directory = TestDirectory::new("detached-stdin-shutdown");
    let temporary_path = test_directory.path().to_path_buf();
    let mut environment = std::env::vars().collect::<std::collections::HashMap<_, _>>();
    environment.insert("TMPDIR".to_string(), temporary_path.display().to_string());

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .arg("--worker")
        .arg(zod)
        .envs(environment);
    let mut control = FixtureControl::new(&mut command);
    control.gate_stdin(&mut command, 3);
    control.control_fixture_cleanup(&mut command);
    let mut client = McpClient::spawn(command);
    control.child_spawned();
    // Start the worker before the detached-child checkpoint so its deadline
    // does not also include lazy worker startup.
    assert_eq!(
        client.call_console(2, json!({"r": "echo ready"})),
        "zod: ready\n"
    );
    client.send_console(
        3,
        json!({
            "r": "stall with detached stdin: 3",
            "stdin": "retained by detached descendant\n",
        }),
    );

    control.wait_for(3, "stdin_write_paused");
    let created = control.wait_for(3, "detached_descendant_created");
    let descendant_pid = created["pid"]
        .as_i64()
        .and_then(|process| libc::pid_t::try_from(process).ok())
        .filter(|process| *process > 0)
        .expect("detached descendant PID should be positive");
    let mut descendant = KillOnDrop::new(descendant_pid);
    assert_eq!(created["inherited_fd"], 0, "{created}");
    assert!(
        created["retained_fd"].as_i64().is_some_and(|fd| fd >= 3),
        "{created}"
    );
    control.wait_for(3, "parent_operation_stalled");

    let elapsed = client.close_within_controlled(Duration::from_secs(2), &mut control);
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
    control.wait_for(3, "stdin_write_cancelled");
    control.cleanup_fixture();
    control.wait_for(3, "fixture_cleanup_completed");
    let cleanup_started = Instant::now();
    loop {
        // SAFETY: this checks only the fixture PID reported for this invocation.
        if unsafe { libc::kill(descendant_pid, 0) } < 0
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        {
            break;
        }
        assert!(
            cleanup_started.elapsed() < Duration::from_secs(2),
            "detached descendant {descendant_pid} did not exit after fixture cleanup"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    descendant.disarm();
}

#[cfg(not(target_os = "macos"))]
#[test]
fn stdio_console_does_not_start_an_unsandboxed_r_session() {
    let mut client = McpClient::start(&["serve"]);

    assert_eq!(
        client.call_console_error(2, json!({"r": "1 + 1"})),
        "workers are supported only on macOS"
    );
}

#[cfg(not(target_os = "macos"))]
#[test]
fn stdio_console_exposes_one_tool_on_unsupported_platforms() {
    let working_directory = TestDirectory::new("unsupported-public-interface");
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .arg("serve")
        .current_dir(working_directory.path())
        .env_remove("MCP_CONSOLE_LANGUAGES");
    let mut client = McpClient::spawn(command);

    let response = client.request(2, "tools/list", None);
    let tools = response["result"]["tools"]
        .as_array()
        .expect("tools/list should return an array");
    assert_eq!(tools.len(), 1, "{response}");
    assert_eq!(tools[0]["name"], "send", "{response}");
    let properties = tools[0]["inputSchema"]["properties"]
        .as_object()
        .expect("send properties should be an object");
    assert_eq!(properties.len(), 7, "{properties:?}");
    for field in [
        "r",
        "python",
        "sql",
        "control",
        "requirements",
        "stdin",
        "timeout_ms",
    ] {
        assert!(
            properties.contains_key(field),
            "missing send field `{field}`"
        );
    }

    assert!(!working_directory.path().join(".mcp-console").exists());
    let removed = client.request(
        3,
        "tools/call",
        Some(json!({
            "name": "session",
            "arguments": {"action": "restart"}
        })),
    );
    assert_eq!(
        removed["error"],
        json!({"code": -32602, "message": "tool not found"}),
        "{removed}"
    );
    assert!(!working_directory.path().join(".mcp-console").exists());

    assert_eq!(
        client.call_console_error(4, json!({"control": "restart"})),
        "[starting new worker]\n[workers are supported only on macOS]"
    );
}

struct McpClient {
    server: Child,
    input: Option<std::process::ChildStdin>,
    output: BufReader<std::process::ChildStdout>,
    closed: bool,
    _working_directory: Option<TestDirectory>,
}

impl McpClient {
    fn start(arguments: &[&str]) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
        command.args(arguments);
        Self::spawn(command)
    }

    fn spawn(mut command: Command) -> Self {
        let working_directory = if command.get_current_dir().is_none() {
            let directory = TestDirectory::new("mcp-server");
            command.current_dir(directory.path());
            Some(directory)
        } else {
            None
        };
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
            _working_directory: working_directory,
        };

        let initialize = client.request(
            1,
            "initialize",
            Some(json!({
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "acceptance-test",
                    "version": "1.0.0"
                }
            })),
        );
        assert_eq!(initialize["result"]["protocolVersion"], "2025-11-25");
        assert_eq!(initialize["result"]["capabilities"], json!({"tools": {}}));
        assert_eq!(initialize["result"]["serverInfo"]["name"], "mcp-console");
        assert_eq!(
            initialize["result"]["serverInfo"]["version"],
            env!("CARGO_PKG_VERSION")
        );

        write_message(
            client.input.as_mut().expect("stdin should be open"),
            &json!({
                "jsonrpc": "2.0",
                "method": "notifications/initialized"
            }),
        );

        client
    }

    fn request(&mut self, id: u64, method: &str, params: Option<Value>) -> Value {
        let mut message = json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": method
        });
        if let Some(params) = params {
            message["params"] = params;
        }
        write_message(self.input.as_mut().expect("stdin should be open"), &message);

        let response = read_message(&mut self.output);
        assert_eq!(response["jsonrpc"], "2.0");
        assert_eq!(response["id"], id);
        response
    }

    fn call_console_response(&mut self, id: u64, arguments: Value) -> Value {
        self.request(
            id,
            "tools/call",
            Some(json!({
                "name": "send",
                "arguments": arguments
            })),
        )
    }

    #[cfg(target_os = "macos")]
    fn send_console(&mut self, id: u64, arguments: Value) {
        self.send_tool(id, "send", arguments);
    }

    #[cfg(unix)]
    fn send_tool(&mut self, id: u64, name: &str, arguments: Value) {
        write_message(
            self.input.as_mut().expect("stdin should be open"),
            &json!({
                "jsonrpc": "2.0",
                "id": id,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": arguments
                }
            }),
        );
    }

    #[cfg(unix)]
    fn cancel(&mut self, id: u64) {
        write_message(
            self.input.as_mut().expect("stdin should be open"),
            &json!({
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": id}
            }),
        );
    }

    #[cfg(target_os = "macos")]
    fn call_console(&mut self, id: u64, arguments: Value) -> String {
        let response = self.call_console_response(id, arguments);
        assert_eq!(response["result"]["isError"], false, "{response}");
        response_text(&response)
    }

    #[cfg(not(target_os = "macos"))]
    fn call_console_error(&mut self, id: u64, arguments: Value) -> String {
        let response = self.call_console_response(id, arguments);
        assert_eq!(response["result"]["isError"], true, "{response}");
        response_text(&response)
    }

    fn close_within(&mut self, timeout: Duration) -> Duration {
        if self.closed {
            return Duration::ZERO;
        }
        drop(self.input.take());
        self.wait_for_exit_within(timeout)
    }

    #[cfg(unix)]
    fn close_controlled(&mut self, control: &mut FixtureControl) {
        if self.closed {
            return;
        }
        drop(self.input.take());
        control.wait_for(0, "transport_closed");
        self.wait_for_exit_within_controlled(Duration::from_secs(3), control);
    }

    #[cfg(unix)]
    fn close_within_controlled(
        &mut self,
        timeout: Duration,
        control: &mut FixtureControl,
    ) -> Duration {
        if self.closed {
            return Duration::ZERO;
        }
        drop(self.input.take());
        self.wait_for_exit_within_controlled(timeout, control)
    }

    fn wait_for_exit_within(&mut self, timeout: Duration) -> Duration {
        self.wait_for_exit_within_context(timeout, String::new)
    }

    #[cfg(unix)]
    fn wait_for_exit_within_controlled(
        &mut self,
        timeout: Duration,
        control: &mut FixtureControl,
    ) -> Duration {
        self.wait_for_exit_within_context(timeout, || control.diagnostics())
    }

    fn wait_for_exit_within_context(
        &mut self,
        timeout: Duration,
        timeout_context: impl FnOnce() -> String,
    ) -> Duration {
        if self.closed {
            return Duration::ZERO;
        }
        let started = Instant::now();
        let status = loop {
            if let Some(status) = self
                .server
                .try_wait()
                .expect("mcp-console status should be readable")
            {
                break status;
            }
            if started.elapsed() >= timeout {
                let _ = self.server.kill();
                let _ = self.server.wait();
                let context = timeout_context();
                panic!("mcp-console did not stop within {timeout:?};{context}");
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
        started.elapsed()
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
            self.close_within(Duration::from_secs(3));
        }
    }
}

fn response_text(response: &Value) -> String {
    assert_eq!(response["result"]["content"][0]["type"], "text");
    response["result"]["content"][0]["text"]
        .as_str()
        .expect("console output should be text")
        .to_owned()
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
    // This path must reach the child unchanged without shell parsing.
    let test_directory = TestDirectory::new("write boundary $(literal)");
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
