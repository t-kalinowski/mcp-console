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
#[cfg(unix)]
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

#[cfg(unix)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum OutputStream {
    Stdout,
    Stderr,
}

#[cfg(unix)]
#[derive(Debug)]
struct OutputLine {
    stream: OutputStream,
    text: String,
}

#[cfg(unix)]
struct RunnerOutput {
    receiver: mpsc::Receiver<OutputLine>,
    readers: Vec<thread::JoinHandle<()>>,
    lines: Vec<OutputLine>,
}

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
impl RunnerOutput {
    fn start(child: &mut ChildProcess) -> Self {
        let (sender, receiver) = mpsc::channel();
        let readers = vec![
            spawn_output_reader(
                child.0.stdout.take().unwrap(),
                OutputStream::Stdout,
                sender.clone(),
            ),
            spawn_output_reader(child.0.stderr.take().unwrap(), OutputStream::Stderr, sender),
        ];
        Self {
            receiver,
            readers,
            lines: Vec::new(),
        }
    }

    fn wait_for(&mut self, description: &str, matches: impl Fn(&OutputLine) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.lines.iter().any(&matches) {
            let remaining = deadline.saturating_duration_since(Instant::now());
            match self.receiver.recv_timeout(remaining) {
                Ok(line) => self.lines.push(line),
                Err(error) => {
                    panic!(
                        "did not observe {description}: {error}; output: {:?}",
                        self.lines
                    );
                }
            }
        }
    }

    fn finish(&mut self) {
        for reader in self.readers.drain(..) {
            reader.join().unwrap();
        }
        self.drain();
    }

    fn drain(&mut self) {
        self.lines.extend(self.receiver.try_iter());
    }

    fn into_streams(mut self) -> (Vec<String>, Vec<String>) {
        self.finish();
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        for line in self.lines {
            match line.stream {
                OutputStream::Stdout => stdout.push(line.text),
                OutputStream::Stderr => stderr.push(line.text),
            }
        }
        (stdout, stderr)
    }
}

#[cfg(unix)]
fn spawn_output_reader(
    stream: impl Read + Send + 'static,
    output_stream: OutputStream,
    sender: mpsc::Sender<OutputLine>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        for line in BufReader::new(stream).lines() {
            sender
                .send(OutputLine {
                    stream: output_stream,
                    text: line.unwrap(),
                })
                .unwrap();
        }
    })
}

#[cfg(unix)]
fn runner_fixture(name: &str, slow_test_seconds: &str) -> (TestDirectory, PathBuf, File) {
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
    assert_eq!(source.matches("SLOW_TEST_SECONDS = 60.0").count(), 1);
    let slow_test_seconds = format!("SLOW_TEST_SECONDS = {slow_test_seconds}");
    fs::write(
        &runner,
        source.replace("SLOW_TEST_SECONDS = 60.0", &slow_test_seconds),
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
fn platform_runner_fixture(name: &str) -> (TestDirectory, PathBuf, PathBuf) {
    let repository = Path::new(env!("CARGO_MANIFEST_DIR"));
    let temporary = TestDirectory::new(name);
    let transcripts = temporary.path().join("tests/transcripts");
    let suite = transcripts.join("client_server/platform.py");
    let golden_directory = transcripts.join("golden/client_server/platform");
    let initialization =
        transcripts.join("golden/client_server/server/initializes_and_lists_tools.yaml");
    let binary = temporary.path().join("target/debug/mcp-console");
    for directory in [
        suite.parent().unwrap(),
        &golden_directory,
        initialization.parent().unwrap(),
        binary.parent().unwrap(),
    ] {
        fs::create_dir_all(directory).unwrap();
    }
    fs::copy(
        repository.join("tests/transcripts/_run.py"),
        transcripts.join("_run.py"),
    )
    .unwrap();
    fs::copy(
        repository.join("tests/transcripts/_support.py"),
        transcripts.join("_support.py"),
    )
    .unwrap();
    fs::copy(
        repository.join("tests/fixtures/transcript_runner/platform.py"),
        &suite,
    )
    .unwrap();
    for case in ["platform_applicable", "platform_inapplicable"] {
        fs::copy(
            repository.join(format!("tests/fixtures/transcript_runner/{case}.yaml")),
            golden_directory.join(format!("{case}.yaml")),
        )
        .unwrap();
    }
    fs::copy(
        repository.join("tests/fixtures/transcript_runner/initializes_and_lists_tools.yaml"),
        initialization,
    )
    .unwrap();
    File::create(binary).unwrap();
    (temporary, transcripts, golden_directory)
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
fn wait_for_runner(mut child: ChildProcess, output: &mut RunnerOutput) -> ExitStatus {
    let process_group = child.0.id() as libc::pid_t;
    let (sender, receiver) = mpsc::channel();
    let waiter = thread::spawn(move || {
        sender.send(child.0.wait()).unwrap();
    });

    match receiver.recv_timeout(Duration::from_secs(10)) {
        Ok(status) => {
            waiter.join().unwrap();
            status.unwrap()
        }
        Err(error) => {
            // SAFETY: `process_group(0)` made the child PID its process-group ID.
            let _ = unsafe { libc::killpg(process_group, libc::SIGKILL) };
            match receiver.recv_timeout(Duration::from_secs(5)) {
                Ok(_) => {
                    waiter.join().unwrap();
                    output.finish();
                    panic!(
                        "transcript test script did not exit: {error}; output: {:?}",
                        output.lines
                    );
                }
                Err(stop_error) => {
                    output.drain();
                    panic!(
                        "transcript test script did not stop after SIGKILL: {stop_error}; \
                         output: {:?}",
                        output.lines
                    );
                }
            }
        }
    }
}

#[cfg(unix)]
#[test]
fn transcript_test_script_reports_one_dot_per_fast_case() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("fast-cases", "30.0");
    release_gate.write_all(b"11").unwrap();
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        1,
        &[
            "client_server/server::initializes_and_lists_tools",
            "client_server/server::blocks_before_queued_case",
        ],
    );
    let mut output = RunnerOutput::start(&mut child);
    let status = wait_for_runner(child, &mut output);
    let (stdout, stderr) = output.into_streams();

    assert!(
        status.success(),
        "transcript test script failed: {stderr:?}"
    );
    assert_eq!(stdout, [".."]);
}

#[cfg(unix)]
#[test]
fn transcript_test_script_preserves_platform_inapplicable_cases() {
    let (temporary, transcripts, golden_directory) =
        platform_runner_fixture("platform-inapplicable");

    let listed = Command::new("uv")
        .args(["run", "--script"])
        .arg(transcripts.join("_run.py"))
        .arg("--list")
        .current_dir(temporary.path())
        .output()
        .expect("list transcript cases");
    assert!(
        listed.status.success(),
        "transcript list failed: {}",
        String::from_utf8_lossy(&listed.stderr)
    );
    assert_eq!(
        String::from_utf8(listed.stdout)
            .unwrap()
            .lines()
            .collect::<Vec<_>>(),
        [
            "client_server/platform::platform_applicable",
            "client_server/platform::platform_inapplicable",
        ]
    );

    let inapplicable = golden_directory.join("platform_inapplicable.yaml");
    let original = fs::read(&inapplicable).unwrap();
    let updated = Command::new("uv")
        .args(["run", "--script"])
        .arg(transcripts.join("_run.py"))
        .args(["--jobs", "1", "--update"])
        .current_dir(temporary.path())
        .output()
        .expect("update transcript cases");
    assert!(
        updated.status.success(),
        "transcript update failed: {}",
        String::from_utf8_lossy(&updated.stderr)
    );
    assert_eq!(fs::read(inapplicable).unwrap(), original);
    let stdout = String::from_utf8(updated.stdout).unwrap();
    assert!(stdout.contains("platform_applicable.yaml"), "{stdout}");
    assert!(!stdout.contains("platform_inapplicable"), "{stdout}");
}

#[cfg(unix)]
#[test]
fn transcript_test_script_reports_a_blocked_case_until_it_finishes() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("slow-case", "0.0");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        1,
        &["client_server/server::initializes_and_lists_tools"],
    );
    let mut output = RunnerOutput::start(&mut child);

    let selector = "client_server/server::initializes_and_lists_tools";
    let running_prefix = format!("{selector}: running for ");
    output.wait_for("slow-case running status", |line| {
        line.stream == OutputStream::Stdout && line.text.starts_with(&running_prefix)
    });

    release_gate.write_all(b"1").unwrap();
    let status = wait_for_runner(child, &mut output);
    let (lines, stderr) = output.into_streams();

    assert!(status.success(), "transcript test failed: {stderr:?}");
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
    let (temporary, transcripts, mut release_gate) = runner_fixture("queued-case", "0.0");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        1,
        &[
            "client_server/server::blocks_before_queued_case",
            "client_server/server::runs_after_blocked_case",
        ],
    );
    let mut output = RunnerOutput::start(&mut child);

    let running_prefix = "client_server/server::blocks_before_queued_case: running for ";
    let queued_running_prefix = "client_server/server::runs_after_blocked_case: running for ";
    output.wait_for("blocked-case running status", |line| {
        line.stream == OutputStream::Stdout && line.text.starts_with(running_prefix)
    });
    assert!(
        output
            .lines
            .iter()
            .all(|line| !line.text.contains("runs_after_blocked_case")),
        "queued case was reported as running: {:?}",
        output.lines
    );

    release_gate.write_all(b"1").unwrap();
    output.wait_for("queued-case running status", |line| {
        line.stream == OutputStream::Stdout && line.text.starts_with(queued_running_prefix)
    });

    release_gate.write_all(b"1").unwrap();
    let status = wait_for_runner(child, &mut output);
    let (lines, stderr) = output.into_streams();

    assert!(status.success(), "transcript test failed: {stderr:?}");
    let prefixes = [
        running_prefix,
        "client_server/server::blocks_before_queued_case: finished in ",
        queued_running_prefix,
        "client_server/server::runs_after_blocked_case: finished in ",
    ];
    assert!(
        lines.iter().all(|line| {
            prefixes.iter().any(|prefix| line.starts_with(prefix))
                || (!line.is_empty() && line.bytes().all(|byte| byte == b'.'))
        }),
        "unexpected stdout: {lines:?}"
    );
    for prefix in prefixes {
        assert_eq!(
            lines.iter().filter(|line| line.starts_with(prefix)).count(),
            1,
            "unexpected stdout: {lines:?}"
        );
    }
    assert_eq!(
        lines
            .iter()
            .filter(|line| !line.is_empty() && line.bytes().all(|byte| byte == b'.'))
            .map(String::len)
            .sum::<usize>(),
        2,
        "unexpected stdout: {lines:?}"
    );
}

#[cfg(unix)]
#[test]
fn transcript_test_script_keeps_reporting_running_cases_after_a_failure() {
    let (temporary, transcripts, mut release_gate) = runner_fixture("failed-sibling", "0.0");
    let mut child = spawn_runner(
        &temporary,
        &transcripts,
        2,
        &[
            "client_server/server::blocks_while_sibling_fails",
            "client_server/server::fails_after_sibling_starts",
        ],
    );
    let mut output = RunnerOutput::start(&mut child);

    let running_prefix = "client_server/server::blocks_while_sibling_fails: running for ";
    let failure_prefix = "client_server/server::fails_after_sibling_starts: failed";
    // Require both causal events without imposing an order across stdout and stderr.
    // A running line alone does not prove that the runner has entered failure cleanup.
    output.wait_for("intentional sibling failure", |line| {
        line.stream == OutputStream::Stderr && line.text.starts_with(failure_prefix)
    });
    output.wait_for("running sibling status after failure", |line| {
        line.stream == OutputStream::Stdout && line.text.starts_with(running_prefix)
    });

    release_gate.write_all(b"1").unwrap();
    let status = wait_for_runner(child, &mut output);
    let (lines, stderr) = output.into_streams();
    let stderr = stderr.join("\n");

    assert!(!status.success(), "fixture failure unexpectedly passed");
    let failing_running_prefix = "client_server/server::fails_after_sibling_starts: running for ";
    let finished_prefix = "client_server/server::blocks_while_sibling_fails: finished in ";
    assert!(
        lines.iter().all(|line| line.starts_with(running_prefix)
            || line.starts_with(failing_running_prefix)
            || line.starts_with(finished_prefix)),
        "unexpected stdout: {lines:?}"
    );
    assert_eq!(
        lines
            .iter()
            .filter(|line| line.starts_with(running_prefix))
            .count(),
        1,
        "unexpected running sibling status: {lines:?}"
    );
    assert_eq!(
        lines
            .iter()
            .filter(|line| line.starts_with(finished_prefix))
            .count(),
        1,
        "missing sibling completion: {lines:?}"
    );
    assert!(
        lines
            .iter()
            .filter(|line| line.starts_with(failing_running_prefix))
            .count()
            <= 1,
        "duplicate failing sibling status: {lines:?}"
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
