use std::os::fd::AsRawFd;
use std::process::{Command, Stdio};

use super::*;

fn receive(reader: &mut crate::sideband::Reader) -> WorkerMessage {
    let mut descriptor = libc::pollfd {
        fd: reader.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let ready = unsafe { libc::poll(&mut descriptor, 1, 10_000) };
    assert!(ready > 0, "native worker did not respond");
    reader.receive().expect("native worker response")
}

fn spawn_probe(
    scenario: &str,
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
    endpoints.configure_process(&mut command);
    let child = command.spawn().expect("start native worker probe");
    (child, reader, writer)
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
