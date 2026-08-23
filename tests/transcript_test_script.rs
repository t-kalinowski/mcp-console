use std::fs;
#[cfg(unix)]
use std::fs::{File, OpenOptions};
#[cfg(unix)]
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
#[cfg(unix)]
use std::process::Child;
use std::process::Command;
#[cfg(unix)]
use std::process::{ExitStatus, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
#[cfg(unix)]
use std::sync::mpsc;
#[cfg(unix)]
use std::thread;
#[cfg(unix)]
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};

struct TestDirectory(PathBuf);

#[cfg(unix)]
struct ChildProcess(Child);

impl TestDirectory {
    fn new(name: &str) -> Self {
        static NEXT_DIRECTORY: AtomicU64 = AtomicU64::new(0);
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after the Unix epoch")
            .as_nanos();
        let sequence = NEXT_DIRECTORY.fetch_add(1, Ordering::Relaxed);
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/transcript-runner-tests")
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
impl Drop for ChildProcess {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_some() {
            return;
        }
        // SAFETY: `process_group(0)` made the child PID its process-group ID.
        let _ = unsafe { libc::killpg(self.0.id() as libc::pid_t, libc::SIGKILL) };
        let _ = self.0.wait();
    }
}

#[cfg(unix)]
fn runner_fixture(name: &str) -> (TestDirectory, PathBuf, File) {
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"));
    let temporary = TestDirectory::new(name);
    let transcripts = temporary.path().join("tests/transcripts");
    let suite = transcripts.join("client_server/server.py");
    let golden = transcripts.join("golden/client_server/server/initializes_and_lists_tools.yaml");
    let binary = temporary.path().join("target/debug/mcp-console");
    for parent in [
        suite.parent().unwrap(),
        golden.parent().unwrap(),
        binary.parent().unwrap(),
    ] {
        fs::create_dir_all(parent).unwrap();
    }
    let runner = transcripts.join("_run.py");
    fs::copy(repository.join("tests/transcripts/_run.py"), &runner).unwrap();
    let source = fs::read_to_string(&runner).unwrap();
    assert_eq!(source.matches("SLOW_TEST_SECONDS = 5.0").count(), 1);
    fs::write(
        &runner,
        source.replace("SLOW_TEST_SECONDS = 5.0", "SLOW_TEST_SECONDS = 0.1"),
    )
    .unwrap();
    fs::copy(
        repository.join("tests/transcripts/_support.py"),
        transcripts.join("_support.py"),
    )
    .unwrap();
    fs::copy(
        repository.join("tests/fixtures/transcript_runner/server.py"),
        &suite,
    )
    .unwrap();
    for name in [
        "initializes_and_lists_tools",
        "blocks_before_queued_case",
        "runs_after_blocked_case",
    ] {
        fs::copy(
            repository.join(format!("tests/fixtures/transcript_runner/{name}.yaml")),
            golden.with_file_name(format!("{name}.yaml")),
        )
        .unwrap();
    }
    File::create(binary).unwrap();

    for name in ["release", "started"] {
        let status = Command::new("mkfifo")
            .arg(temporary.path().join(name))
            .status()
            .unwrap();
        assert!(status.success(), "mkfifo failed for {name}: {status}");
    }
    let release_gate = OpenOptions::new()
        .read(true)
        .write(true)
        .open(temporary.path().join("release"))
        .unwrap();
    (temporary, transcripts, release_gate)
}

#[cfg(unix)]
fn spawn_runner(
    temporary: &TestDirectory,
    transcripts: &Path,
    jobs: usize,
    selectors: &[&str],
) -> ChildProcess {
    let mut command = Command::new("uv");
    command
        .args(["run", "--script"])
        .arg(transcripts.join("_run.py"))
        .arg("--jobs")
        .arg(jobs.to_string())
        .args(selectors)
        .current_dir(temporary.path())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    ChildProcess(
        command
            .spawn()
            .expect("transcript test script should start"),
    )
}

#[cfg(unix)]
fn wait_for_runner(mut child: ChildProcess) -> (ExitStatus, String) {
    let process_group = child.0.id() as libc::pid_t;
    let mut child_stderr = child.0.stderr.take().unwrap();
    let stderr_reader = thread::spawn(move || {
        let mut stderr = String::new();
        child_stderr.read_to_string(&mut stderr).unwrap();
        stderr
    });
    let (sender, receiver) = mpsc::channel();
    let waiter = thread::spawn(move || {
        sender.send(child.0.wait()).unwrap();
    });

    match receiver.recv_timeout(Duration::from_secs(10)) {
        Ok(status) => {
            waiter.join().unwrap();
            (status.unwrap(), stderr_reader.join().unwrap())
        }
        Err(error) => {
            // SAFETY: `process_group(0)` made the child PID its process-group ID.
            let _ = unsafe { libc::killpg(process_group, libc::SIGKILL) };
            let _ = receiver.recv_timeout(Duration::from_secs(5));
            waiter.join().unwrap();
            let stderr = stderr_reader.join().unwrap();
            panic!("transcript test script did not exit: {error}; stderr: {stderr}");
        }
    }
}

#[test]
fn transcript_test_script_reports_one_dot_per_fast_case() {
    let root = env!("CARGO_MANIFEST_DIR");
    let runner = format!("{root}/tests/transcripts/_run.py");
    let output = Command::new("uv")
        .args(["run", "--script", &runner, "--jobs", "1", "cli/help"])
        .current_dir(root)
        .output()
        .expect("transcript test script should run");

    assert!(
        output.status.success(),
        "transcript test script failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(String::from_utf8(output.stdout).unwrap(), "..\n");
}

#[cfg(unix)]
#[test]
fn transcript_test_script_reports_a_blocked_case_until_it_finishes() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("slow-case");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        1,
        &["client_server/server::initializes_and_lists_tools"],
    );
    let stdout = child.0.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            sender.send(line.unwrap()).unwrap();
        }
    });

    let selector = "client_server/server::initializes_and_lists_tools";
    let running_prefix = format!("{selector}: running for ");
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut lines = Vec::new();
    let observed = loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining) {
            Ok(line) => {
                let matched = line.starts_with(&running_prefix);
                lines.push(line);
                if matched {
                    break true;
                }
            }
            Err(_) => break false,
        }
    };
    if !observed {
        panic!("slow status was not reported; stdout lines: {lines:?}");
    }

    release_gate.write_all(b"1").unwrap();
    let (status, stderr) = wait_for_runner(child);
    reader.join().unwrap();
    lines.extend(receiver.try_iter());

    assert!(status.success(), "transcript test failed: {stderr}");
    assert!(
        lines[0].starts_with(&running_prefix),
        "unexpected stdout: {lines:?}"
    );
    assert!(
        lines[1].starts_with(&format!("{selector}: finished in ")),
        "unexpected completion line: {:?}",
        lines[1]
    );
    assert_eq!(lines[2], ".");
    assert_eq!(lines.len(), 3, "unexpected stdout: {lines:?}");
}

#[cfg(unix)]
#[test]
fn transcript_test_script_does_not_time_cases_waiting_for_a_worker() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("queued-case");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        1,
        &[
            "client_server/server::blocks_before_queued_case",
            "client_server/server::runs_after_blocked_case",
        ],
    );
    let stdout = child.0.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            sender.send(line.unwrap()).unwrap();
        }
    });

    let running_prefix = "client_server/server::blocks_before_queued_case: running for ";
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut lines = Vec::new();
    let observed = loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining) {
            Ok(line) => {
                let matched = line.starts_with(running_prefix);
                lines.push(line);
                if matched {
                    break true;
                }
            }
            Err(_) => break false,
        }
    };
    if !observed {
        panic!("running case was not reported: {lines:?}");
    }
    assert!(
        lines
            .iter()
            .all(|line| !line.contains("runs_after_blocked_case")),
        "queued case was reported as running: {lines:?}"
    );

    release_gate.write_all(b"1").unwrap();
    let (status, stderr) = wait_for_runner(child);
    reader.join().unwrap();
    lines.extend(receiver.try_iter());

    assert!(status.success(), "transcript test failed: {stderr}");
    assert_eq!(lines.len(), 3, "unexpected stdout: {lines:?}");
    assert!(lines[0].starts_with(running_prefix));
    assert!(
        lines[1].starts_with("client_server/server::blocks_before_queued_case: finished in "),
        "missing completion line: {lines:?}"
    );
    assert_eq!(lines[2], "..");
}

#[cfg(unix)]
#[test]
fn transcript_test_script_keeps_reporting_running_cases_after_a_failure() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("failed-sibling");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        2,
        &[
            "client_server/server::blocks_while_sibling_fails",
            "client_server/server::fails_after_sibling_starts",
        ],
    );
    let stdout = child.0.stdout.take().unwrap();
    let (sender, receiver) = mpsc::channel();
    let reader = thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            sender.send(line.unwrap()).unwrap();
        }
    });

    let running_prefix = "client_server/server::blocks_while_sibling_fails: running for ";
    let deadline = Instant::now() + Duration::from_secs(10);
    let mut lines = Vec::new();
    let observed = loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        match receiver.recv_timeout(remaining) {
            Ok(line) => {
                let matched = line.starts_with(running_prefix);
                lines.push(line);
                if matched {
                    break true;
                }
            }
            Err(_) => break false,
        }
    };
    if !observed {
        panic!("running sibling was not reported after failure: {lines:?}");
    }

    release_gate.write_all(b"1").unwrap();
    let (status, stderr) = wait_for_runner(child);
    reader.join().unwrap();
    lines.extend(receiver.try_iter());

    assert!(!status.success(), "fixture failure unexpectedly passed");
    assert_eq!(lines.len(), 2, "unexpected stdout: {lines:?}");
    assert!(lines[0].starts_with(running_prefix));
    assert!(
        lines[1].starts_with("client_server/server::blocks_while_sibling_fails: finished in "),
        "missing sibling completion: {lines:?}"
    );
    assert!(
        stderr.contains("client_server/server::fails_after_sibling_starts: failed"),
        "missing failed case in stderr: {stderr}"
    );
    assert!(
        stderr.contains("fixture failure"),
        "unexpected stderr: {stderr}"
    );
}
