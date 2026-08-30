#![cfg(any(target_os = "macos", target_os = "linux"))]

use std::collections::BTreeSet;
use std::fs;
#[cfg(target_os = "macos")]
use std::io::Read as _;
use std::io::Write as _;
use std::os::unix::ffi::OsStringExt as _;
use std::os::unix::fs::PermissionsExt as _;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::Value;

struct TestDirectory(PathBuf);

impl TestDirectory {
    fn new(name: &str) -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/sandbox-runner-tests")
            .join(format!(
                "{name}-{}-{unique}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(&path).expect("create test directory");
        Self(path)
    }
}

impl Drop for TestDirectory {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove test directory");
    }
}

fn fake_runner_process(behavior: &str, target: &str) -> (Command, TestDirectory, PathBuf) {
    let directory = TestDirectory::new(behavior);
    let log = directory.0.join("runner.json");
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/sandbox_runner.py");
    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command
        .args(["sandbox", "--", target])
        .env("MCP_CONSOLE_TEST_SANDBOX_RUNNER", fixture)
        .env("MCP_CONSOLE_FAKE_RUNNER_BEHAVIOR", behavior)
        .env("MCP_CONSOLE_FAKE_RUNNER_LOG", &log)
        .env(
            "MCP_CONSOLE_EXPECTED_CODEX_REVISION",
            runner_pin()["commit"].as_str().expect("runner pin commit"),
        );
    (command, directory, log)
}

fn fake_runner_command(behavior: &str, target: &str) -> (Output, Value) {
    let (mut command, _directory, log) = fake_runner_process(behavior, target);
    let output = command.output().expect("run mcp-console");
    (output, runner_log(&log))
}

fn runner_log(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).expect("read fake runner log"))
        .expect("parse fake runner log")
}

fn requests(log: &Value) -> &[Value] {
    log["requests"].as_array().expect("runner requests")
}

fn request_types(log: &Value) -> Vec<&str> {
    requests(log)
        .iter()
        .map(|request| request["type"].as_str().expect("request type"))
        .collect()
}

fn cleanup_path(log: &Value) -> PathBuf {
    PathBuf::from(
        log["bootstrap"]["cleanup_directory"]
            .as_str()
            .expect("cleanup directory"),
    )
}

fn runner_pin() -> Value {
    serde_json::from_str(include_str!("../sandbox-runner.json")).expect("parse sandbox runner pin")
}

#[test]
fn discovers_and_launches_the_exact_private_runner() {
    let (output, log) = fake_runner_command("success", "/usr/bin/true");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        request_types(&log),
        ["discover", "launch", "status", "wait"]
    );
    let requests = requests(&log);
    assert_eq!(requests[0]["protocol_version"], 1);
    assert_eq!(requests[1]["launch"]["network"]["mode"], "denied");
    assert_eq!(
        requests[1]["launch"]["lifecycle"]["force_timeout_ms"],
        2_000
    );
    assert_eq!(requests[3]["retirement_timeout_ms"], 7_000);
    assert_eq!(
        requests[1]["launch"]["filesystem"]["base"],
        "host_read_only"
    );
    assert_eq!(
        log["bootstrap"]["target"][0].as_str(),
        Some("/usr/bin/true")
    );
    let state = Path::new(
        log["bootstrap"]["state_directory"]
            .as_str()
            .expect("state directory"),
    );
    let cleanup = cleanup_path(&log);
    assert!(state.is_absolute());
    assert!(cleanup.is_absolute());
    assert_ne!(state, cleanup);
    assert_eq!(
        log["bootstrap"]["runner_process_group"], log["bootstrap"]["runner_pid"],
        "the private runner must not share the launcher process group"
    );

    let control = log["bootstrap"]["control_fd"]
        .as_i64()
        .expect("control descriptor");
    let streams = log["bootstrap"]["stream_fds"]
        .as_array()
        .expect("stream descriptors")
        .iter()
        .map(|descriptor| descriptor.as_i64().expect("stream descriptor"))
        .collect::<Vec<_>>();
    assert_eq!(streams.len(), 3);
    assert_eq!(streams.iter().copied().collect::<BTreeSet<_>>().len(), 3);
    assert!(!streams.contains(&control));
    assert!(streams.iter().all(|descriptor| *descriptor >= 64));
    assert_eq!(
        requests[1]["launch"]["streams"]
            .as_object()
            .expect("launch streams")
            .values()
            .map(|stream| stream["handle"].as_i64().expect("stream handle"))
            .collect::<BTreeSet<_>>(),
        streams.into_iter().collect()
    );
    assert_eq!(log["events"], serde_json::json!(["cleanup_before_final"]));
    assert!(!cleanup.exists());
}

#[test]
#[cfg(target_os = "linux")]
fn closes_an_inheritable_descriptor_opened_while_forking() {
    let (mut command, directory, log) =
        fake_runner_process("late_inherited_descriptor", "/usr/bin/true");
    let interposer = directory.0.join("open-fd-at-fork.so");
    let source = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/open_fd_at_fork.c");
    let compilation = Command::new("cc")
        .args([
            "-shared", "-fPIC", "-std=c11", "-Wall", "-Wextra", "-Werror", "-o",
        ])
        .arg(&interposer)
        .arg(source)
        .output()
        .expect("compile at-fork descriptor fixture");
    assert!(
        compilation.status.success(),
        "{}",
        String::from_utf8_lossy(&compilation.stderr)
    );
    let marker = directory.0.join("inherited-descriptor");
    let output = command
        .env("LD_PRELOAD", interposer)
        .env("MCP_CONSOLE_TEST_AT_FORK_FD_PATH", &marker)
        .env("MCP_CONSOLE_TEST_AT_FORK_FD", "211")
        .output()
        .expect("run mcp-console");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        request_types(&runner_log(&log)),
        ["discover", "launch", "status", "wait"]
    );
    assert_eq!(
        fs::read(marker).expect("read inherited descriptor marker"),
        b""
    );
}

#[test]
fn resolves_the_private_runner_from_a_symlinked_installation() {
    let directory = TestDirectory::new("symlinked-installation");
    let tool = directory.0.join("tool");
    let tool_bin = tool.join("bin");
    let libexec = tool.join("libexec");
    let public_bin = directory.0.join("public-bin");
    fs::create_dir_all(&tool_bin).expect("create installed bin directory");
    fs::create_dir(&libexec).expect("create installed libexec directory");
    fs::create_dir(&public_bin).expect("create public bin directory");

    let installed = tool_bin.join("mcp-console");
    fs::copy(env!("CARGO_BIN_EXE_mcp-console"), &installed).expect("copy installed mcp-console");
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/sandbox_runner.py");
    let runner = libexec.join("mcp-console-sandbox");
    fs::copy(fixture, &runner).expect("copy private runner fixture");
    fs::set_permissions(&runner, fs::Permissions::from_mode(0o755))
        .expect("make private runner fixture executable");
    let public = public_bin.join("mcp-console");
    symlink(&installed, &public).expect("link public mcp-console command");

    let log = directory.0.join("runner.json");
    let output = Command::new(public)
        .args(["sandbox", "--", "/usr/bin/true"])
        .env_remove("MCP_CONSOLE_TEST_SANDBOX_RUNNER")
        .env("MCP_CONSOLE_FAKE_RUNNER_BEHAVIOR", "success")
        .env("MCP_CONSOLE_FAKE_RUNNER_LOG", &log)
        .env(
            "MCP_CONSOLE_EXPECTED_CODEX_REVISION",
            runner_pin()["commit"].as_str().expect("runner pin commit"),
        )
        .output()
        .expect("run symlinked mcp-console");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        request_types(&runner_log(&log)),
        ["discover", "launch", "status", "wait"]
    );
}

#[test]
fn rejects_mismatched_runner_identity_and_capabilities_before_launch() {
    for (behavior, message) in [
        ("wrong_protocol", "protocol_version mismatch"),
        ("wrong_frame_size", "maximum_frame_size mismatch"),
        ("wrong_revision", "codex_source_revision mismatch"),
        ("unsupported_backend", "backend mismatch"),
        ("wrong_lifecycle", "capability lifecycle.interrupt"),
        ("unexpected_companion", "companion layout"),
    ] {
        let (output, log) = fake_runner_command(behavior, "/usr/bin/true");
        assert!(!output.status.success(), "{behavior}");
        assert_eq!(request_types(&log), ["discover"], "{behavior}: {log}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains(message),
            "{behavior}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            log["events"],
            serde_json::json!(["cleanup_after_control_eof"])
        );
        assert!(!cleanup_path(&log).exists());
    }
}

#[test]
fn rejects_a_launch_response_from_the_wrong_backend() {
    let (output, log) = fake_runner_command("wrong_launch_backend", "/usr/bin/true");

    assert!(!output.status.success());
    assert_eq!(request_types(&log), ["discover", "launch"]);
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("launch backend mismatch"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!cleanup_path(&log).exists());
}

#[test]
#[cfg(target_os = "macos")]
fn rejects_a_launch_response_without_a_root_process_identifier() {
    let (output, log) = fake_runner_command("missing_root_process_id", "/usr/bin/true");

    assert!(!output.status.success());
    assert_eq!(request_types(&log), ["discover", "launch"]);
    assert!(
        String::from_utf8_lossy(&output.stderr)
            .contains("launch omitted the target process identifier"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(!cleanup_path(&log).exists());
}

#[test]
fn closes_and_reaps_on_malformed_control_and_runner_exit() {
    let started = Instant::now();
    for behavior in [
        "exit_before_discovery",
        "malformed_discovery",
        "truncated_discovery",
        "exit_after_launch",
        "malformed_status",
        "truncated_status",
    ] {
        let (output, log) = fake_runner_command(behavior, "/usr/bin/true");
        assert!(!output.status.success(), "{behavior}");
        assert!(!output.stderr.is_empty(), "{behavior}");
        let cleanup = cleanup_path(&log);
        if behavior.contains("launch") || behavior.contains("status") {
            assert!(
                cleanup.is_dir(),
                "{behavior}: target state was not preserved"
            );
            fs::remove_dir_all(cleanup).expect("remove preserved target directory");
        } else {
            assert!(!cleanup.exists(), "{behavior}: pre-launch state remained");
        }
    }
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "control failures waited for protocol timeouts"
    );
}

#[test]
fn control_loss_retires_the_generation_before_returning() {
    let (output, log) = fake_runner_command("control_loss", "/usr/bin/true");

    assert!(!output.status.success());
    assert_eq!(request_types(&log), ["discover", "launch"]);
    assert_eq!(
        log["events"],
        serde_json::json!(["cleanup_before_control_loss_exit"])
    );
    assert!(!cleanup_path(&log).exists());
}

#[test]
fn a_delayed_status_response_does_not_retire_the_target() {
    let (output, log) = fake_runner_command("delayed_status", "/usr/bin/true");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        request_types(&log),
        ["discover", "launch", "status", "wait"]
    );
}

#[test]
fn a_stalled_status_response_fails_at_the_control_deadline() {
    let (output, log) = fake_runner_command("stalled_status", "/usr/bin/true");

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("status response timed out"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(request_types(&log), ["discover", "launch", "status"]);
    assert_eq!(
        log["events"],
        serde_json::json!(["cleanup_after_status_timeout"])
    );
    assert!(!cleanup_path(&log).exists());
}

#[test]
fn rejects_an_unknown_status_phase_before_waiting() {
    let (output, log) = fake_runner_command("invalid_status_phase", "/usr/bin/true");

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("unexpected phase future"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(request_types(&log), ["discover", "launch", "status"]);
    assert_eq!(
        log["events"],
        serde_json::json!(["cleanup_after_control_eof"])
    );
    assert!(!cleanup_path(&log).exists());
}

#[test]
#[cfg(target_os = "macos")]
fn rejects_an_interrupt_acknowledgment_for_another_operation() {
    let (mut command, _directory, log_path) =
        fake_runner_process("wrong_interrupt_ack", "/usr/bin/true");
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().expect("start mcp-console");
    let mut ready = [0_u8; 6];
    child
        .stdout
        .as_mut()
        .expect("mcp-console stdout")
        .read_exact(&mut ready)
        .expect("read runner readiness");
    assert_eq!(&ready, b"ready\n");
    assert_eq!(
        unsafe { libc::kill(child.id() as libc::pid_t, libc::SIGINT) },
        0
    );
    let output = child.wait_with_output().expect("wait for mcp-console");
    let log = runner_log(&log_path);

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("acknowledgment mismatch"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(request_types(&log), ["discover", "launch", "interrupt"]);
}

#[test]
fn direct_standard_streams_preserve_bytes_separation_and_eof() {
    let (mut command, _directory, log_path) =
        fake_runner_process("direct_streams", "/usr/bin/true");
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().expect("start mcp-console");
    let input = b"input:\x00\xff\n";
    child
        .stdin
        .take()
        .expect("mcp-console stdin")
        .write_all(input)
        .expect("write binary input");
    let output = child.wait_with_output().expect("wait for mcp-console");
    let log = runner_log(&log_path);

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        output.stdout,
        [b"stdout:\x00".as_slice(), input, b"\xff"].concat()
    );
    assert_eq!(
        output.stderr,
        [b"stderr:\x00".as_slice(), input, b"\xfe"].concat()
    );
    assert_eq!(log["events"], serde_json::json!(["cleanup_before_final"]));
}

#[test]
fn target_exit_and_signal_outcomes_are_preserved() {
    let (exited, _) = fake_runner_command("target_exit", "/usr/bin/true");
    let (signaled, _) = fake_runner_command("target_signal", "/usr/bin/true");

    assert_eq!(exited.status.code(), Some(17));
    assert_eq!(signaled.status.code(), Some(128 + libc::SIGTERM));
}

#[test]
fn rejects_malformed_target_outcomes() {
    for behavior in [
        "missing_exit_code",
        "missing_signal",
        "exited_with_signal",
        "signaled_with_code",
    ] {
        let (output, _) = fake_runner_command(behavior, "/usr/bin/true");
        assert!(!output.status.success(), "{behavior}");
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("invalid target outcome"),
            "{behavior}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}

#[test]
fn cleanup_failure_preserves_diagnostic_state() {
    let (output, log) = fake_runner_command("cleanup_failure", "/usr/bin/true");
    let cleanup = cleanup_path(&log);

    assert!(!output.status.success());
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("fixture cleanup failed"),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(cleanup.is_dir());
    fs::remove_dir_all(cleanup).expect("remove preserved target directory");
}

#[test]
fn resolves_bare_targets_with_executable_path_semantics() {
    let (mut command, _directory, log_path) = fake_runner_process("success", "path-target");
    let work = log_path.parent().expect("test directory");
    let first = work.join("first");
    let second = work.join("second");
    fs::create_dir(&first).expect("create first PATH directory");
    fs::create_dir(&second).expect("create second PATH directory");
    let shadow = first.join("path-target");
    let selected = second.join("path-target");
    fs::write(&shadow, "not executable").expect("write non-executable target");
    fs::set_permissions(&shadow, fs::Permissions::from_mode(0o011))
        .expect("make the owner-denied target look executable by mode bits");
    fs::write(&selected, "#!/bin/sh\nexit 0\n").expect("write executable target");
    fs::set_permissions(&selected, fs::Permissions::from_mode(0o755))
        .expect("make target executable");
    let path = std::env::join_paths(
        [PathBuf::from("first"), PathBuf::from("second")]
            .into_iter()
            .chain(std::env::split_paths(
                &std::env::var_os("PATH").unwrap_or_default(),
            )),
    )
    .expect("test PATH");
    command.current_dir(work).env("PATH", path);
    let output = command.output().expect("run mcp-console");
    let log = runner_log(&log_path);

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(log["bootstrap"]["target"][0].as_str(), selected.to_str());
}

#[test]
fn preserves_non_utf8_target_arguments() {
    let (mut command, _directory, log_path) = fake_runner_process("success", "/usr/bin/true");
    command.arg(std::ffi::OsString::from_vec(b"argument-\xff".to_vec()));
    let output = command.output().expect("run mcp-console");

    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let log = runner_log(&log_path);
    assert_eq!(
        log["bootstrap"]["target_bytes"][1].as_str(),
        Some("617267756d656e742dff")
    );
}

#[test]
fn private_runner_lookup_never_uses_path() {
    let directory = TestDirectory::new("path-lookup");
    let bin = directory.0.join("bin");
    let libexec = directory.0.join("libexec");
    fs::create_dir(&bin).expect("create bin directory");
    fs::create_dir(&libexec).expect("create libexec directory");
    let installed = bin.join("mcp-console");
    fs::copy(env!("CARGO_BIN_EXE_mcp-console"), &installed).expect("copy mcp-console");
    let marker = directory.0.join("path-runner-used");
    let path_runner = bin.join("mcp-console-sandbox");
    fs::write(
        &path_runner,
        format!("#!/bin/sh\n: > '{}'\nexit 0\n", marker.display()),
    )
    .expect("write PATH runner");
    fs::set_permissions(&path_runner, fs::Permissions::from_mode(0o755))
        .expect("make PATH runner executable");

    let output = Command::new(installed)
        .args(["sandbox", "--", "/usr/bin/true"])
        .env("PATH", &bin)
        .env_remove("MCP_CONSOLE_TEST_SANDBOX_RUNNER")
        .output()
        .expect("run copied mcp-console");

    assert!(!output.status.success());
    assert!(!marker.exists(), "runner was searched on PATH");
}
