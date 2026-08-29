use std::io::{Read as _, Write as _};
use std::os::unix::net::UnixStream;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::{Value, json};

const MAXIMUM_FRAME_SIZE: usize = 1_048_576;
const STATUS_RESPONSE_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy)]
struct PendingStatus {
    id: u64,
    deadline: Instant,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct RunnerPin {
    pub repository: String,
    pub release: String,
    pub commit: String,
    pub protocol_version: u32,
    pub rust_toolchain: String,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct FinalOutcome {
    pub target: Option<TargetOutcome>,
    pub retirement: RetirementOutcome,
    pub infrastructure: InfrastructureOutcome,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct TargetOutcome {
    pub kind: String,
    pub code: Option<i64>,
    pub signal: Option<i32>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct RetirementOutcome {
    pub complete: bool,
    pub error: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub(super) struct InfrastructureOutcome {
    pub error: Option<String>,
    pub cleanup_error: Option<String>,
}

pub(super) struct Control {
    writer: UnixStream,
    responses: Receiver<Result<Value, String>>,
    reader: Option<JoinHandle<()>>,
    next_id: u64,
    protocol_version: u32,
    pending_status: Option<PendingStatus>,
}

impl Control {
    pub(super) fn new(stream: UnixStream, protocol_version: u32) -> Result<Self, String> {
        let mut reader = stream
            .try_clone()
            .map_err(|error| format!("failed to duplicate private sandbox control: {error}"))?;
        let (sender, responses) = mpsc::channel();
        let reader = thread::Builder::new()
            .name("sandbox-control-reader".to_string())
            .spawn(move || {
                loop {
                    let response = read_frame(&mut reader);
                    let failed = response.is_err();
                    if sender.send(response).is_err() || failed {
                        break;
                    }
                }
            })
            .map_err(|error| format!("failed to start private sandbox control reader: {error}"))?;
        Ok(Self {
            writer: stream,
            responses,
            reader: Some(reader),
            next_id: 1,
            protocol_version,
            pending_status: None,
        })
    }

    pub(super) fn discover(&mut self) -> Result<Value, String> {
        self.request("discover", json!({}), Duration::from_secs(10))
    }

    pub(super) fn launch(&mut self, launch: Value) -> Result<Value, String> {
        self.request(
            "launch",
            json!({ "launch": launch }),
            Duration::from_secs(30),
        )
    }

    pub(super) fn status(&mut self, timeout: Duration) -> Result<Option<Value>, String> {
        let result = self.status_open(timeout);
        if result.is_err() {
            self.close();
        }
        result
    }

    pub(super) fn interrupt(&mut self) -> Result<Value, String> {
        self.request("interrupt", json!({}), Duration::from_secs(2))
    }

    pub(super) fn terminate(&mut self, graceful_ms: u64, force_ms: u64) -> Result<Value, String> {
        self.request(
            "terminate",
            json!({
                "deadlines": {
                    "graceful_ms": graceful_ms,
                    "force_ms": force_ms,
                }
            }),
            Duration::from_millis(graceful_ms.saturating_add(force_ms).saturating_add(1_000)),
        )
    }

    pub(super) fn wait(&mut self, retirement_timeout_ms: u64) -> Result<Value, String> {
        self.request(
            "wait",
            json!({ "retirement_timeout_ms": retirement_timeout_ms }),
            Duration::from_millis(retirement_timeout_ms.saturating_add(1_000)),
        )
    }

    pub(super) fn close(&mut self) {
        let _ = self.writer.shutdown(std::net::Shutdown::Both);
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }

    fn request(
        &mut self,
        operation: &str,
        fields: Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        let result = self.request_open(operation, fields, timeout);
        if result.is_err() {
            self.close();
        }
        result
    }

    fn request_open(
        &mut self,
        operation: &str,
        fields: Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        let id = self.send_request(operation, fields, timeout)?;
        self.receive_required_response(id, timeout)
    }

    fn status_open(&mut self, timeout: Duration) -> Result<Option<Value>, String> {
        let id = match self.pending_status {
            Some(status) => status.id,
            None => {
                let id = self.send_request("status", json!({}), STATUS_RESPONSE_TIMEOUT)?;
                self.pending_status = Some(PendingStatus {
                    id,
                    deadline: Instant::now() + STATUS_RESPONSE_TIMEOUT,
                });
                id
            }
        };
        self.receive_response(id, timeout)
    }

    fn send_request(
        &mut self,
        operation: &str,
        fields: Value,
        timeout: Duration,
    ) -> Result<u64, String> {
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or_else(|| "private sandbox request identifiers were exhausted".to_string())?;
        let mut request = fields
            .as_object()
            .cloned()
            .expect("request fields should be an object");
        request.insert("type".to_string(), Value::String(operation.to_string()));
        request.insert("id".to_string(), Value::from(id));
        request.insert(
            "protocol_version".to_string(),
            Value::from(self.protocol_version),
        );

        let timeout = timeout.max(Duration::from_millis(1));
        self.writer
            .set_write_timeout(Some(timeout))
            .map_err(|error| format!("failed to configure private sandbox control: {error}"))?;
        write_frame(&mut self.writer, &Value::Object(request))?;
        Ok(id)
    }

    fn receive_required_response(&mut self, id: u64, timeout: Duration) -> Result<Value, String> {
        self.receive_response(id, timeout)?
            .ok_or_else(|| format!("private sandbox response {id} timed out"))
    }

    fn receive_response(&mut self, id: u64, timeout: Duration) -> Result<Option<Value>, String> {
        let deadline = Instant::now() + timeout;
        loop {
            let now = Instant::now();
            let response_deadline = self
                .pending_status
                .map(|status| status.deadline.min(deadline))
                .unwrap_or(deadline);
            let remaining = response_deadline.saturating_duration_since(now);
            let response = match self.responses.recv_timeout(remaining) {
                Ok(response) => response?,
                Err(RecvTimeoutError::Timeout) => {
                    let now = Instant::now();
                    if self
                        .pending_status
                        .is_some_and(|status| now >= status.deadline)
                    {
                        return Err("private sandbox status response timed out".to_string());
                    }
                    return Ok(None);
                }
                Err(RecvTimeoutError::Disconnected) => {
                    return Err("private sandbox control reader stopped".to_string());
                }
            };
            let response_id = response.get("id").and_then(Value::as_u64);
            if response_id == Some(id) {
                if self.pending_status.is_some_and(|status| status.id == id) {
                    self.pending_status = None;
                }
                return validate_response(response, id).map(Some);
            }
            if response_id == self.pending_status.map(|status| status.id) {
                let pending_id = self
                    .pending_status
                    .take()
                    .expect("matched pending status response should have state")
                    .id;
                let response = validate_response(response, pending_id)?;
                expect_response_type(&response, "status")?;
                continue;
            }
            return Err(format!(
                "private sandbox response correlation mismatch: expected {id}, got {}",
                response
                    .get("id")
                    .map(Value::to_string)
                    .unwrap_or_else(|| "missing".to_string())
            ));
        }
    }
}

impl Drop for Control {
    fn drop(&mut self) {
        self.close();
    }
}

fn validate_response(response: Value, id: u64) -> Result<Value, String> {
    let response_id = response.get("id").and_then(Value::as_u64);
    if response_id != Some(id) {
        return Err(format!(
            "private sandbox response correlation mismatch: expected {id}, got {}",
            response
                .get("id")
                .map(Value::to_string)
                .unwrap_or_else(|| "missing".to_string())
        ));
    }
    if response.get("type").and_then(Value::as_str) == Some("error") {
        let error = &response["error"];
        let code = error["code"].as_str().unwrap_or("unknown");
        let phase = error["phase"].as_str().unwrap_or("unknown");
        let message = error["message"]
            .as_str()
            .unwrap_or("the private sandbox runner returned an invalid error");
        return Err(format!(
            "private sandbox {phase} failed ({code}): {message}"
        ));
    }
    Ok(response)
}

pub(super) fn pin() -> Result<RunnerPin, String> {
    let pin: RunnerPin = serde_json::from_str(include_str!("../../sandbox-runner.json"))
        .map_err(|error| format!("invalid private sandbox provenance: {error}"))?;
    if pin.protocol_version != 1
        || pin.commit.len() != 40
        || !pin.commit.bytes().all(|byte| byte.is_ascii_hexdigit())
        || pin.repository.is_empty()
        || pin.release.is_empty()
        || pin.rust_toolchain.is_empty()
    {
        return Err("invalid private sandbox provenance".to_string());
    }
    Ok(pin)
}

pub(super) fn validate_capabilities(response: &Value, pin: &RunnerPin) -> Result<(), String> {
    expect_response_type(response, "capabilities")?;
    let capabilities = response
        .get("capabilities")
        .ok_or_else(|| "private sandbox discovery omitted capabilities".to_string())?;
    expect_number(
        capabilities,
        "protocol_version",
        u64::from(pin.protocol_version),
    )?;
    expect_number(
        capabilities,
        "maximum_frame_size",
        MAXIMUM_FRAME_SIZE as u64,
    )?;
    expect_text(capabilities, "codex_source_revision", &pin.commit)?;
    expect_text(capabilities, "codex_release_tag", &pin.release)?;

    let expected_backend = "macos_seatbelt";
    expect_text(capabilities, "backend", expected_backend)?;
    let setup = capabilities
        .get("setup")
        .ok_or_else(|| "private sandbox discovery omitted setup state".to_string())?;
    let setup_state = setup
        .get("state")
        .and_then(Value::as_str)
        .unwrap_or("missing");
    if !matches!(setup_state, "not_required" | "ready") {
        let detail = setup
            .get("detail")
            .and_then(Value::as_str)
            .unwrap_or("no diagnostic was provided");
        return Err(format!(
            "private sandbox backend is unavailable ({setup_state}): {detail}"
        ));
    }

    for (section, fields) in [
        (
            "filesystem",
            &[
                "host_read_only",
                "write_rules",
                "deny_read_rules",
                "deny_write_rules",
                "missing_path_error",
                "missing_path_ignore",
                "state_directory_protected",
            ][..],
        ),
        ("network", &["denied", "direct_egress_confinement"][..]),
        (
            "streams",
            &["passed_handle", "independent", "byte_transparent"][..],
        ),
        (
            "terminal",
            &[
                "inherited_terminal",
                "caller_supplied_pty",
                "pty_creation_inside_sandbox",
            ][..],
        ),
        (
            "lifecycle",
            &[
                "forced_termination",
                "root_exit_observation",
                "process_tree_supervision",
                "full_tree_retirement",
                "cleanup_after_root_exit",
                "control_loss_retires_target",
            ][..],
        ),
    ] {
        for field in fields {
            expect_bool(capabilities, section, field, true)?;
        }
    }
    expect_bool(
        capabilities,
        "streams",
        "application_bytes_on_control_channel",
        false,
    )?;
    expect_bool(capabilities, "terminal", "host_device_isolation", false)?;
    expect_text(
        &capabilities["filesystem"],
        "precedence",
        "more_specific_then_deny_then_write_then_read",
    )?;

    expect_bool(capabilities, "lifecycle", "interrupt", true)?;
    expect_bool(capabilities, "lifecycle", "graceful_termination", true)?;
    validate_companions(capabilities, &[])?;
    Ok(())
}

pub(super) fn expect_response_type(response: &Value, expected: &str) -> Result<(), String> {
    let actual = response
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("missing");
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "private sandbox returned {actual} while {expected} was required"
        ))
    }
}

pub(super) fn expect_launch_accepted(response: &Value) -> Result<(), String> {
    expect_response_type(response, "launch_accepted")?;
    let backend = response
        .get("backend")
        .and_then(Value::as_str)
        .unwrap_or("missing");
    if backend == "macos_seatbelt" {
        Ok(())
    } else {
        Err(format!(
            "private sandbox launch backend mismatch: expected macos_seatbelt, got {backend}"
        ))
    }
}

pub(super) fn expect_acknowledgment(response: &Value, operation: &str) -> Result<(), String> {
    expect_response_type(response, "acknowledged")?;
    let actual = response
        .get("operation")
        .and_then(Value::as_str)
        .unwrap_or("missing");
    if actual == operation {
        Ok(())
    } else {
        Err(format!(
            "private sandbox acknowledgment mismatch: expected {operation}, got {actual}"
        ))
    }
}

pub(super) fn final_outcome(response: Value) -> Result<FinalOutcome, String> {
    expect_response_type(&response, "final")?;
    serde_json::from_value(
        response
            .get("outcome")
            .cloned()
            .ok_or_else(|| "private sandbox final response omitted its outcome".to_string())?,
    )
    .map_err(|error| format!("private sandbox returned an invalid final outcome: {error}"))
}

fn write_frame(stream: &mut UnixStream, value: &Value) -> Result<(), String> {
    let payload = serde_json::to_vec(value)
        .map_err(|error| format!("failed to encode private sandbox request: {error}"))?;
    if payload.is_empty() || payload.len() > MAXIMUM_FRAME_SIZE {
        return Err("private sandbox request exceeded its protocol limit".to_string());
    }
    let length = u32::try_from(payload.len())
        .expect("private sandbox frame limit should fit in a u32")
        .to_be_bytes();
    stream
        .write_all(&length)
        .and_then(|()| stream.write_all(&payload))
        .map_err(|error| format!("failed to write private sandbox control: {error}"))
}

fn read_frame(stream: &mut UnixStream) -> Result<Value, String> {
    let mut length = [0_u8; 4];
    stream.read_exact(&mut length).map_err(|error| {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            "private sandbox runner closed its control channel".to_string()
        } else {
            format!("failed to read private sandbox control header: {error}")
        }
    })?;
    let length = u32::from_be_bytes(length) as usize;
    if length == 0 || length > MAXIMUM_FRAME_SIZE {
        return Err(format!(
            "private sandbox runner returned an invalid frame length {length}"
        ));
    }
    let mut payload = vec![0; length];
    stream.read_exact(&mut payload).map_err(|error| {
        if error.kind() == std::io::ErrorKind::UnexpectedEof {
            "private sandbox runner truncated a control frame".to_string()
        } else {
            format!("failed to read private sandbox control payload: {error}")
        }
    })?;
    serde_json::from_slice(&payload)
        .map_err(|error| format!("private sandbox runner returned malformed JSON: {error}"))
}

fn expect_bool(value: &Value, section: &str, field: &str, expected: bool) -> Result<(), String> {
    let actual = value
        .get(section)
        .and_then(|section| section.get(field))
        .and_then(Value::as_bool);
    if actual == Some(expected) {
        Ok(())
    } else {
        Err(format!(
            "private sandbox capability {section}.{field} must be {expected}"
        ))
    }
}

fn expect_text(value: &Value, field: &str, expected: &str) -> Result<(), String> {
    let actual = value
        .get(field)
        .and_then(Value::as_str)
        .unwrap_or("missing");
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "private sandbox {field} mismatch: expected {expected}, got {actual}"
        ))
    }
}

fn expect_number(value: &Value, field: &str, expected: u64) -> Result<(), String> {
    let actual = value.get(field).and_then(Value::as_u64);
    if actual == Some(expected) {
        Ok(())
    } else {
        Err(format!(
            "private sandbox {field} mismatch: expected {expected}, got {}",
            actual.map_or_else(|| "missing".to_string(), |value| value.to_string())
        ))
    }
}

fn validate_companions(capabilities: &Value, expected: &[(&str, &str)]) -> Result<(), String> {
    let companions = capabilities
        .get("required_companions")
        .and_then(Value::as_array)
        .ok_or_else(|| "private sandbox discovery omitted required companions".to_string())?;
    let actual = companions
        .iter()
        .filter(|companion| companion["required"].as_bool() == Some(true))
        .map(|companion| {
            (
                companion["name"].as_str().unwrap_or("missing"),
                companion["relative_path"].as_str().unwrap_or("missing"),
            )
        })
        .collect::<Vec<_>>();
    if actual == expected {
        Ok(())
    } else {
        Err("private sandbox companion layout does not match this installation".to_string())
    }
}
