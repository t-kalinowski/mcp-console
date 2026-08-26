use serde::Serialize;

#[cfg(unix)]
use std::os::fd::RawFd;
#[cfg(unix)]
use std::sync::OnceLock;

#[cfg(unix)]
const EVENT_FD_ENV: &str = "MCP_CONSOLE_TEST_EVENT_FD";
#[cfg(unix)]
const RESPONSE_GATE_FD_ENV: &str = "MCP_CONSOLE_TEST_RESPONSE_GATE_FD";
#[cfg(unix)]
const RESPONSE_GATE_OPERATION_ENV: &str = "MCP_CONSOLE_TEST_RESPONSE_GATE_OPERATION";
#[cfg(unix)]
const STDIN_GATE_FD_ENV: &str = "MCP_CONSOLE_TEST_STDIN_GATE_FD";
#[cfg(unix)]
const STDIN_GATE_OPERATION_ENV: &str = "MCP_CONSOLE_TEST_STDIN_GATE_OPERATION";
#[cfg(unix)]
const STDOUT_OBSERVED_ENV: &str = "MCP_CONSOLE_TEST_STDOUT_OBSERVED";
#[cfg(unix)]
const POSIX_MINIMUM_ATOMIC_PIPE_WRITE: usize = 512;

#[cfg(unix)]
static EVENT_FD: OnceLock<Option<RawFd>> = OnceLock::new();
#[cfg(unix)]
static RESPONSE_GATE: OnceLock<Option<ResponseGate>> = OnceLock::new();
#[cfg(unix)]
static STDIN_GATE: OnceLock<Option<(u64, RawFd)>> = OnceLock::new();
#[cfg(unix)]
static STDOUT_OBSERVED: OnceLock<Result<Option<std::path::PathBuf>, String>> = OnceLock::new();

#[derive(Clone)]
pub(crate) struct Operation(rmcp::model::RequestId);

#[cfg(unix)]
struct ResponseGate {
    operation: rmcp::model::RequestId,
    descriptor: RawFd,
}

#[derive(Serialize)]
struct Event<'a, T: Serialize + ?Sized> {
    operation: &'a T,
    kind: &'a str,
    component: &'a str,
}

impl Operation {
    pub(crate) fn new(request_id: rmcp::model::RequestId) -> Self {
        Self(request_id)
    }

    pub(crate) fn emit(&self, kind: &str) {
        emit(&self.0, kind, "server");
    }
}

pub(crate) fn emit<T: Serialize + ?Sized>(operation: &T, kind: &str, component: &str) {
    #[cfg(unix)]
    {
        let Some(descriptor) = event_descriptor() else {
            return;
        };
        let mut payload = serde_json::to_vec(&Event {
            operation,
            kind,
            component,
        })
        .expect("test event should serialize");
        payload.push(b'\n');
        assert!(
            payload.len() <= POSIX_MINIMUM_ATOMIC_PIPE_WRITE,
            "test event exceeds the POSIX minimum atomic pipe write"
        );
        write_all_atomic(descriptor, &payload);
    }

    #[cfg(not(unix))]
    let _ = (operation, kind, component);
}

pub(crate) async fn wait_for_response_gate(operation: &rmcp::model::RequestId) {
    #[cfg(unix)]
    {
        let Some(gate) = response_gate() else {
            return;
        };
        if &gate.operation != operation {
            return;
        }
        emit(operation, "response_write_paused", "transport");
        let descriptor = gate.descriptor;
        tokio::task::spawn_blocking(move || wait_for_release(descriptor))
            .await
            .expect("response gate task should complete");
    }

    #[cfg(not(unix))]
    let _ = operation;
}

#[cfg(unix)]
pub(crate) fn stdin_gate() -> Option<(u64, RawFd)> {
    *STDIN_GATE.get_or_init(|| {
        let operation = std::env::var_os(STDIN_GATE_OPERATION_ENV);
        let descriptor = std::env::var_os(STDIN_GATE_FD_ENV);
        match (operation, descriptor) {
            (None, None) => None,
            (Some(operation), Some(descriptor)) => {
                let operation = operation
                    .into_string()
                    .expect("stdin gate operation must be valid UTF-8")
                    .parse()
                    .expect("stdin gate operation must be an unsigned integer");
                let descriptor = parse_descriptor(descriptor, STDIN_GATE_FD_ENV);
                Some((operation, descriptor))
            }
            _ => panic!(
                "{STDIN_GATE_OPERATION_ENV} and {STDIN_GATE_FD_ENV} must be configured together"
            ),
        }
    })
}

#[cfg(unix)]
pub(crate) fn acknowledge_stdout_observed() -> Result<(), String> {
    let Some(path) = stdout_observed_path()? else {
        return Ok(());
    };
    std::fs::write(path, b"")
        .map_err(|error| format!("test stdout observation {} failed: {error}", path.display()))
}

#[cfg(unix)]
fn stdout_observed_path() -> Result<Option<&'static std::path::Path>, String> {
    let configured = STDOUT_OBSERVED.get_or_init(|| {
        let Some(name) = std::env::var_os(STDOUT_OBSERVED_ENV) else {
            return Ok(None);
        };
        let name = std::path::Path::new(&name);
        assert!(
            name.components().count() == 1,
            "{STDOUT_OBSERVED_ENV} must be one path component"
        );
        let temporary = std::env::var_os("TMPDIR")
            .ok_or_else(|| format!("{STDOUT_OBSERVED_ENV} requires TMPDIR"))?;
        Ok(Some(std::path::PathBuf::from(temporary).join(name)))
    });
    configured
        .as_ref()
        .map(|path| path.as_deref())
        .map_err(Clone::clone)
}

#[cfg(unix)]
fn event_descriptor() -> Option<RawFd> {
    *EVENT_FD.get_or_init(|| configured_descriptor(EVENT_FD_ENV))
}

#[cfg(unix)]
fn response_gate() -> Option<&'static ResponseGate> {
    RESPONSE_GATE
        .get_or_init(|| {
            let operation = std::env::var_os(RESPONSE_GATE_OPERATION_ENV);
            let descriptor = std::env::var_os(RESPONSE_GATE_FD_ENV);
            match (operation, descriptor) {
                (None, None) => None,
                (Some(operation), Some(descriptor)) => {
                    let operation = operation
                        .into_string()
                        .expect("response gate operation must be valid UTF-8");
                    let operation = serde_json::from_str(&operation)
                        .expect("response gate operation must be a JSON request ID");
                    let descriptor = parse_descriptor(descriptor, RESPONSE_GATE_FD_ENV);
                    Some(ResponseGate {
                        operation,
                        descriptor,
                    })
                }
                _ => panic!(
                    "{RESPONSE_GATE_OPERATION_ENV} and {RESPONSE_GATE_FD_ENV} must be configured together"
                ),
            }
        })
        .as_ref()
}

#[cfg(unix)]
fn configured_descriptor(name: &str) -> Option<RawFd> {
    std::env::var_os(name).map(|value| parse_descriptor(value, name))
}

#[cfg(unix)]
fn parse_descriptor(value: std::ffi::OsString, name: &str) -> RawFd {
    let value = value
        .into_string()
        .unwrap_or_else(|_| panic!("{name} must be valid UTF-8"));
    let descriptor = value
        .parse::<RawFd>()
        .unwrap_or_else(|_| panic!("{name} must be a file descriptor"));
    assert!(
        descriptor >= 0,
        "{name} must be a nonnegative file descriptor"
    );
    descriptor
}

#[cfg(unix)]
fn write_all_atomic(descriptor: RawFd, payload: &[u8]) {
    loop {
        // SAFETY: `payload` remains valid for this call, and the descriptor was
        // explicitly inherited through the test environment.
        let written = unsafe { libc::write(descriptor, payload.as_ptr().cast(), payload.len()) };
        if written < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted
        {
            continue;
        }
        assert_eq!(
            written,
            payload.len() as libc::ssize_t,
            "test event write should complete atomically: {}",
            std::io::Error::last_os_error()
        );
        return;
    }
}

#[cfg(unix)]
fn wait_for_release(descriptor: RawFd) {
    let mut byte = 0_u8;
    loop {
        // SAFETY: `byte` remains valid for this call, and the descriptor was
        // explicitly inherited through the test environment.
        let read = unsafe { libc::read(descriptor, (&mut byte as *mut u8).cast(), 1) };
        if read < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        assert!(
            matches!(read, 0 | 1),
            "response gate read should return one byte or EOF: {}",
            std::io::Error::last_os_error()
        );
        return;
    }
}
