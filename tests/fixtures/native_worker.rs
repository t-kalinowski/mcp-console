use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use super::*;

fn receive(reader: &mut crate::sideband::Reader) -> WorkerMessage {
    receive_bounded(reader)
        .expect("native worker response")
        .expect("native worker did not respond")
}

fn receive_bounded(reader: &mut crate::sideband::Reader) -> io::Result<Option<WorkerMessage>> {
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(message) = reader.receive_buffered()? {
            return Ok(Some(message));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Ok(None);
        }
        let timeout = remaining.as_millis().try_into().unwrap_or(i32::MAX);
        let mut descriptor = libc::pollfd {
            fd: reader.as_raw_fd(),
            events: libc::POLLIN,
            revents: 0,
        };
        match unsafe { libc::poll(&mut descriptor, 1, timeout) } {
            0 => return Ok(None),
            -1 if io::Error::last_os_error().kind() == io::ErrorKind::Interrupted => continue,
            -1 => return Err(io::Error::last_os_error()),
            _ => match reader.read_chunk() {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => continue,
                Err(error) => return Err(error),
            },
        }
    }
}

fn receive_python_probe(reader: &mut crate::sideband::Reader, child: &mut Child) -> WorkerMessage {
    let reason = match receive_bounded(reader) {
        Ok(Some(message)) => return message,
        Ok(None) => "timed out waiting for a sideband message".to_owned(),
        Err(error) => format!("sideband receive failed: {error}"),
    };
    let _ = child.kill();
    let status = child.wait().expect("native Python probe exit");
    let mut stderr = Vec::new();
    child
        .stderr
        .take()
        .expect("native Python probe stderr")
        .read_to_end(&mut stderr)
        .expect("read native Python probe stderr");
    panic!(
        "native Python probe {reason} (exit {status}): {}",
        String::from_utf8_lossy(&stderr)
    );
}

fn spawn_probe(
    scenario: &str,
) -> (
    std::process::Child,
    crate::sideband::Reader,
    crate::sideband::Writer,
) {
    spawn_probe_with_configuration(scenario, None)
}

fn spawn_probe_with_configuration(
    scenario: &str,
    configuration: Option<&str>,
) -> (
    std::process::Child,
    crate::sideband::Reader,
    crate::sideband::Writer,
) {
    let (reader, writer, endpoints) = crate::sideband::bind().expect("bind sideband");
    let mut command = Command::new(std::env::current_exe().expect("test executable"));
    command
        .args([
            "--exact",
            "worker::coordinator::tests::native_probe",
            "--nocapture",
        ])
        .env("MCP_CONSOLE_NATIVE_PROBE", scenario)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped());
    if let Some(configuration) = configuration {
        command.env("MCP_CONSOLE_NATIVE_PYTHON_CONFIG", configuration);
    }
    endpoints.configure_process(&mut command);
    let child = command.spawn().expect("start native worker probe");
    (child, reader, writer)
}

#[test]
fn native_python_setup_runs_cells_without_r() {
    let fixture = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/embedded_python_config.py"
    );
    let configuration = Command::new("python3")
        .arg(fixture)
        .output()
        .expect("run known Python fixture");
    assert!(configuration.status.success());
    let configuration = String::from_utf8(configuration.stdout).expect("fixture configuration");
    let (mut child, mut reader, _writer) =
        spawn_probe_with_configuration("python_setup", Some(&configuration));
    assert!(matches!(
        receive_python_probe(&mut reader, &mut child),
        WorkerMessage::Ready
    ));

    let mut cells = Vec::new();
    for _ in 0..6 {
        let mut output = String::new();
        let mut diagnostic = String::new();
        loop {
            let message = receive_python_probe(&mut reader, &mut child);
            match message {
                WorkerMessage::ConsoleOutput { data } => output.push_str(&data),
                WorkerMessage::ConsoleDiagnostic { data } => diagnostic.push_str(&data),
                WorkerMessage::Completed => break,
                _ => panic!("unexpected native Python response"),
            }
        }
        cells.push((output, diagnostic));
    }
    assert_eq!(cells[0].0, "42\n");
    assert_eq!(cells[1].0, "43\n");
    assert!(cells[2].1.contains("ValueError: native setup failure"));
    assert_eq!(cells[3].0, "44\n");
    assert_eq!(cells[4], (String::new(), String::new()));
    assert!(
        cells[5]
            .1
            .contains("Automatic resolution disabled in native fixture")
    );
    assert!(
        child
            .wait_with_output()
            .expect("native Python probe exit")
            .status
            .success()
    );
}

#[test]
fn native_command_wait_and_shutdown() {
    let (child, mut reader, writer) = spawn_probe("command");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    writer
        .send(&ServerMessage::Shutdown)
        .expect("send shutdown");
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_interrupt_wakes_and_acknowledges_once() {
    let (child, mut reader, writer) = spawn_probe("interrupt");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    let result = unsafe { libc::kill(child.id() as i32, libc::SIGINT) };
    assert_eq!(result, 0, "send interrupt");
    assert!(
        matches!(receive(&mut reader), WorkerMessage::ConsoleOutput { data } if data == "acknowledged")
    );
    writer
        .send(&ServerMessage::Shutdown)
        .expect("send shutdown");
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_wait_observes_stdin_shutdown() {
    let (mut child, mut reader, _writer) = spawn_probe("stdin");
    assert!(matches!(receive(&mut reader), WorkerMessage::Ready));
    drop(child.stdin.take());
    assert!(matches!(receive(&mut reader), WorkerMessage::Completed));
    assert!(
        child
            .wait_with_output()
            .expect("probe exit")
            .status
            .success()
    );
}

#[test]
fn native_probe() {
    let Ok(scenario) = std::env::var("MCP_CONSOLE_NATIVE_PROBE") else {
        return;
    };
    let (reader, writer) = crate::sideband::connect_from_env().expect("connect sideband");
    core::initialize(reader, writer.clone()).expect("initialize sideband");
    let integration = Integration::new(None).expect("initialize native integration");
    writer
        .send(&WorkerMessage::Ready)
        .expect("report readiness");

    if scenario == "python_setup" {
        let name = if cfg!(target_os = "macos") {
            c"libR.dylib"
        } else {
            c"libR.so"
        };
        let r_library = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) };
        assert!(r_library.is_null(), "libR was loaded before Python setup");
        let configuration: crate::python::SelectedPython = serde_json::from_str(
            &std::env::var("MCP_CONSOLE_NATIVE_PYTHON_CONFIG").expect("fixture configuration"),
        )
        .expect("parse fixture configuration");
        crate::python::initialize_selected(&configuration).expect("initialize known Python");
        assert!(
            crate::python::setup_runtime(
                std::path::Path::new(&configuration.libpython),
                crate::python::ImportResolution {
                    callback: None,
                    disabled_reason: Some("Automatic resolution disabled in native fixture"),
                },
            )
            .expect("install native Python setup")
        );
        crate::python::finish_initialization().expect("release initial Python GIL");
        for (index, source) in [
            r#"native_value = 41
native_value + 1"#,
            r#"native_value += 2
native_value"#,
            r#"raise ValueError('native setup failure')"#,
            r#"native_value += 1
native_value"#,
            r#"import _mcp_console

def fail_reconfiguration(*arguments):
    raise AssertionError('configured runtime was installed twice')

_mcp_console.configure_import_resolution = fail_reconfiguration"#,
            r#"import mcp_console_missing_native_fixture_package"#,
        ]
        .into_iter()
        .enumerate()
        {
            crate::python::evaluate_embedded(source, &format!("<native-python:{index}>"))
                .expect("evaluate native Python cell");
            if index == 4 {
                assert!(
                    crate::python::setup_runtime(
                        std::path::Path::new(&configuration.libpython),
                        crate::python::ImportResolution {
                            callback: None,
                            disabled_reason: Some(
                                "Automatic resolution disabled in native fixture"
                            ),
                        },
                    )
                    .expect("reuse completed Python setup")
                );
            }
            writer
                .send(&WorkerMessage::Completed)
                .expect("report completed Python cell");
        }
        let r_library = unsafe { libc::dlopen(name.as_ptr(), libc::RTLD_NOLOAD | libc::RTLD_NOW) };
        assert!(r_library.is_null(), "libR was loaded during Python setup");
        return;
    }

    if scenario == "interrupt" {
        let descriptor = match core::next_command().expect("next command") {
            CommandReadiness::Waiting(descriptor) => descriptor,
            CommandReadiness::Ready(_) => panic!("unexpected command"),
        };
        assert!(!interrupt::wait_for_activity(descriptor).expect("wait for interrupt"));
        let acknowledgments = std::thread::scope(|scope| {
            let first = scope.spawn(interrupt::acknowledge_python_interrupt);
            let second = scope.spawn(interrupt::acknowledge_python_interrupt);
            usize::from(first.join().expect("first acknowledgment"))
                + usize::from(second.join().expect("second acknowledgment"))
        });
        assert_eq!(acknowledgments, 1);
        assert!(!interrupt::acknowledge_python_interrupt());
        writer
            .send(&WorkerMessage::ConsoleOutput {
                data: "acknowledged".to_string(),
            })
            .expect("report acknowledgment");
    }

    assert!(matches!(
        Coordinator::wait_for_message(&integration).expect("wait for shutdown"),
        ServerMessage::Shutdown
    ));
    writer
        .send(&WorkerMessage::Completed)
        .expect("report shutdown");
}
