use std::fs;
use std::process::{Child, Stdio};
use std::time::{Duration, Instant};

use amalthea::session::Session;
use amalthea::socket::Socket;
use amalthea::wire::execute_request::{
    ExecuteRequest, ExecuteRequestPositron, JupyterPositronLocation, JupyterPositronPosition,
    JupyterPositronRange,
};
use amalthea::wire::input_reply::InputReply;
use amalthea::wire::jupyter_message::{JupyterMessage, Message, ProtocolMessage, Status};
use amalthea::wire::kernel_info_request::KernelInfoRequest;
use amalthea::wire::shutdown_request::ShutdownRequest;
use amalthea::wire::status::ExecutionState;
use rand::RngExt;
use serde_json::{Value, json};
use tempfile::TempDir;

use crate::sandbox;

const STARTUP_TIMEOUT: Duration = Duration::from_secs(30);
const POLL_INTERVAL: Duration = Duration::from_millis(100);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(1);
const ARK_SESSION_ROOT: &str = "/tmp";
const JUPYTER_PROTOCOL_VERSION: &str = "5.4";
const CONTROL_CHANNEL: u16 = 1;
const SHELL_CHANNEL: u16 = 2;
const STDIN_CHANNEL: u16 = 3;
const IOPUB_CHANNEL: u16 = 4;
const HEARTBEAT_CHANNEL: u16 = 5;

pub struct ArkKernel {
    child: Child,
    frontend: Frontend,
    state: KernelState,
    active_message_id: Option<String>,
    pending_input: String,
    evaluation_count: u64,
    log_path: std::path::PathBuf,
    _directory: TempDir,
}

struct Frontend {
    _context: zmq::Context,
    session: Session,
    control: Socket,
    shell: Socket,
    iopub: Socket,
    stdin: Socket,
}

enum KernelState {
    Idle,
    Running,
    Input { prompt: String },
    Stopped { diagnostic: String },
}

impl ArkKernel {
    pub fn start() -> Result<Self, String> {
        let directory = tempfile::Builder::new()
            .prefix("mcp-console-ark-")
            .tempdir_in(ARK_SESSION_ROOT)
            .map_err(|error| format!("failed to create ark session directory: {error}"))?;
        let directory_path = directory
            .path()
            .canonicalize()
            .map_err(|error| format!("failed to resolve ark session directory: {error}"))?;
        let connection_path = directory_path.join("connection.json");
        let log_path = directory_path.join("ark.log");
        let endpoint_prefix = directory_path.join("jupyter");
        let endpoint_prefix = endpoint_prefix
            .to_str()
            .ok_or_else(|| String::from("ark session directory is not UTF-8"))?;

        let key = hex::encode(rand::rng().random::<[u8; 32]>());
        let session = Session::create(&key)
            .map_err(|error| format!("failed to create Jupyter session: {error}"))?;
        let context = zmq::Context::new();
        let connection_file = json!({
            "transport": "ipc",
            "ip": endpoint_prefix,
            "signature_scheme": "hmac-sha256",
            "key": key,
            "control_port": CONTROL_CHANNEL,
            "shell_port": SHELL_CHANNEL,
            "stdin_port": STDIN_CHANNEL,
            "iopub_port": IOPUB_CHANNEL,
            "hb_port": HEARTBEAT_CHANNEL
        });
        fs::write(
            &connection_path,
            serde_json::to_vec(&connection_file)
                .map_err(|error| format!("failed to encode ark connection file: {error}"))?,
        )
        .map_err(|error| format!("failed to write ark connection file: {error}"))?;

        let log = fs::File::create(&log_path)
            .map_err(|error| format!("failed to create ark log: {error}"))?;
        let log_stdout = log
            .try_clone()
            .map_err(|error| format!("failed to open ark log for stdout: {error}"))?;
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate mcp-console worker executable: {error}"))?;
        let mut command = sandbox::worker_command(executable.as_os_str(), &directory_path)?;
        let mut child = command
            .arg("worker")
            .arg(&connection_path)
            .env("RUST_LOG", "error")
            .stdin(Stdio::null())
            .stdout(Stdio::from(log_stdout))
            .stderr(Stdio::from(log))
            .spawn()
            .map_err(|error| format!("failed to start ark: {error}"))?;

        let frontend =
            match connect_frontend(context, session, endpoint_prefix, &mut child, &log_path) {
                Ok(frontend) => frontend,
                Err(error) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(error);
                }
            };

        Ok(Self {
            child,
            frontend,
            state: KernelState::Idle,
            active_message_id: None,
            pending_input: String::new(),
            evaluation_count: 0,
            log_path,
            _directory: directory,
        })
    }

    pub fn evaluate(&mut self, code: String) -> Result<String, String> {
        match &self.state {
            KernelState::Idle => {}
            KernelState::Running | KernelState::Input { .. } => {
                return Err(String::from(
                    "cannot evaluate R code while the session is waiting for stdin",
                ));
            }
            KernelState::Stopped { diagnostic } => return Err(diagnostic.clone()),
        }

        self.evaluation_count = self
            .evaluation_count
            .checked_add(1)
            .ok_or_else(|| String::from("R evaluation counter overflowed"))?;
        let positron = ExecuteRequestPositron {
            code_location: Some(code_location(
                &self.frontend.session.session_id,
                self.evaluation_count,
                &code,
            )?),
            ..Default::default()
        };
        let message_id = match send(
            &self.frontend.shell,
            &self.frontend.session,
            ExecuteRequest {
                code,
                silent: false,
                store_history: true,
                user_expressions: Value::Null,
                allow_stdin: true,
                stop_on_error: false,
                positron: Some(positron),
            },
        ) {
            Ok(message_id) => message_id,
            Err(error) => return Err(self.mark_stopped(error)),
        };
        self.active_message_id = Some(message_id);
        self.state = KernelState::Running;
        self.pending_input.clear();
        self.wait_for_boundary()
    }

    pub fn provide_input(&mut self, input: String) -> Result<String, String> {
        let prompt = match &self.state {
            KernelState::Input { prompt } => prompt.clone(),
            KernelState::Idle | KernelState::Running => {
                return Err(String::from("stdin is accepted only at an R input prompt"));
            }
            KernelState::Stopped { diagnostic } => return Err(diagnostic.clone()),
        };

        self.pending_input.push_str(&input);
        let Some(line) = self.take_input_line() else {
            return Ok(render_input(String::new(), &prompt));
        };

        if let Err(error) = self.send_input_reply(line) {
            return Err(self.mark_stopped(error));
        }
        self.state = KernelState::Running;
        self.wait_for_boundary()
    }

    fn wait_for_boundary(&mut self) -> Result<String, String> {
        match self.wait_for_boundary_inner() {
            Ok(output) => Ok(output),
            Err(error) => Err(self.mark_stopped(error)),
        }
    }

    fn wait_for_boundary_inner(&mut self) -> Result<String, String> {
        let mut output = String::new();
        let mut shell_done = false;
        let mut idle = false;

        loop {
            self.check_child()?;

            let (iopub_ready, stdin_ready, shell_ready) = {
                let mut items = [
                    self.frontend.iopub.socket.as_poll_item(zmq::POLLIN),
                    self.frontend.stdin.socket.as_poll_item(zmq::POLLIN),
                    self.frontend.shell.socket.as_poll_item(zmq::POLLIN),
                ];
                zmq::poll(&mut items, POLL_INTERVAL.as_millis() as i64)
                    .map_err(|error| format!("failed to poll ark sockets: {error}"))?;
                (
                    items[0].is_readable(),
                    items[1].is_readable(),
                    items[2].is_readable(),
                )
            };

            if iopub_ready {
                self.drain_iopub(&mut output, &mut idle)?;
            }

            let mut prompt = None;
            if stdin_ready {
                prompt = self.drain_stdin()?;
            }

            if shell_ready {
                shell_done = self.drain_shell()? || shell_done;
            }

            if let Some(prompt) = prompt {
                self.drain_iopub(&mut output, &mut idle)?;

                if let Some(line) = self.take_input_line() {
                    self.send_input_reply(line)?;
                    continue;
                }

                self.state = KernelState::Input {
                    prompt: prompt.clone(),
                };
                return Ok(render_input(output, &prompt));
            }

            if shell_done && idle {
                self.state = KernelState::Idle;
                self.active_message_id = None;
                self.pending_input.clear();
                return Ok(render_done(output));
            }
        }
    }

    fn drain_iopub(&self, output: &mut String, idle: &mut bool) -> Result<(), String> {
        while self
            .frontend
            .iopub
            .has_incoming_data()
            .map_err(|error| format!("failed to inspect ark IOPub socket: {error}"))?
        {
            let message = Message::read_from_socket(&self.frontend.iopub)
                .map_err(|error| format!("failed to read ark IOPub message: {error}"))?;
            if !self.is_active(&message) {
                continue;
            }

            match message {
                Message::Status(message)
                    if message.content.execution_state == ExecutionState::Idle =>
                {
                    *idle = true;
                }
                Message::Stream(message) => output.push_str(&message.content.text),
                Message::ExecuteResult(message) => {
                    append_plain_text(output, &message.content.data);
                }
                Message::ExecuteError(message) => {
                    append_block(output, &message.content.exception.evalue);
                    for traceback in &message.content.exception.traceback {
                        append_block(output, traceback);
                    }
                }
                Message::DisplayData(message) => {
                    append_plain_text(output, &message.content.data);
                }
                Message::UpdateDisplayData(message) => {
                    append_plain_text(output, &message.content.data);
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn drain_stdin(&self) -> Result<Option<String>, String> {
        let mut prompt = None;
        while self
            .frontend
            .stdin
            .has_incoming_data()
            .map_err(|error| format!("failed to inspect ark stdin socket: {error}"))?
        {
            let message = Message::read_from_socket(&self.frontend.stdin)
                .map_err(|error| format!("failed to read ark stdin message: {error}"))?;
            if !self.is_active(&message) {
                continue;
            }
            if let Message::InputRequest(message) = message {
                prompt = Some(message.content.prompt);
            }
        }
        Ok(prompt)
    }

    fn drain_shell(&self) -> Result<bool, String> {
        let mut done = false;
        while self
            .frontend
            .shell
            .has_incoming_data()
            .map_err(|error| format!("failed to inspect ark shell socket: {error}"))?
        {
            let message = Message::read_from_socket(&self.frontend.shell)
                .map_err(|error| format!("failed to read ark shell message: {error}"))?;
            if !self.is_active(&message) {
                continue;
            }
            if matches!(
                message,
                Message::ExecuteReply(_) | Message::ExecuteReplyException(_)
            ) {
                done = true;
            }
        }
        Ok(done)
    }

    fn is_active(&self, message: &Message) -> bool {
        let Some(active_message_id) = &self.active_message_id else {
            return false;
        };
        message
            .parent_header()
            .is_some_and(|parent| parent.msg_id == *active_message_id)
    }

    fn send_input_reply(&self, value: String) -> Result<(), String> {
        send(
            &self.frontend.stdin,
            &self.frontend.session,
            InputReply { value },
        )
        .map(|_| ())
    }

    fn take_input_line(&mut self) -> Option<String> {
        let newline = self.pending_input.find('\n')?;
        let mut line = self.pending_input.drain(..=newline).collect::<String>();
        line.pop();
        if line.ends_with('\r') {
            line.pop();
        }
        Some(line)
    }

    fn check_child(&mut self) -> Result<(), String> {
        let Some(status) = self
            .child
            .try_wait()
            .map_err(|error| format!("failed to inspect ark process: {error}"))?
        else {
            return Ok(());
        };
        Err(format_child_exit(status, &self.log_path))
    }

    fn mark_stopped(&mut self, reason: String) -> String {
        let diagnostic = format!("[stopped: {reason}]");
        self.state = KernelState::Stopped {
            diagnostic: diagnostic.clone(),
        };
        self.active_message_id = None;
        self.pending_input.clear();
        diagnostic
    }
}

impl Drop for ArkKernel {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_some() {
            return;
        }

        let _ = send(
            &self.frontend.control,
            &self.frontend.session,
            ShutdownRequest { restart: false },
        );
        let deadline = Instant::now() + SHUTDOWN_TIMEOUT;
        while Instant::now() < deadline {
            if self.child.try_wait().ok().flatten().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(25));
        }

        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn connect_frontend(
    context: zmq::Context,
    session: Session,
    endpoint_prefix: &str,
    child: &mut Child,
    log_path: &std::path::Path,
) -> Result<Frontend, String> {
    let identity = session.session_id.as_bytes();
    let control = Socket::new(
        session.clone(),
        context.clone(),
        String::from("Control"),
        zmq::DEALER,
        None,
        endpoint(endpoint_prefix, CONTROL_CHANNEL),
    )
    .map_err(|error| format!("failed to connect ark control socket: {error}"))?;
    let shell = Socket::new(
        session.clone(),
        context.clone(),
        String::from("Shell"),
        zmq::DEALER,
        Some(identity),
        endpoint(endpoint_prefix, SHELL_CHANNEL),
    )
    .map_err(|error| format!("failed to connect ark shell socket: {error}"))?;
    let iopub = Socket::new(
        session.clone(),
        context.clone(),
        String::from("IOPub"),
        zmq::SUB,
        None,
        endpoint(endpoint_prefix, IOPUB_CHANNEL),
    )
    .map_err(|error| format!("failed to connect ark IOPub socket: {error}"))?;
    let stdin = Socket::new(
        session.clone(),
        context.clone(),
        String::from("Stdin"),
        zmq::DEALER,
        Some(identity),
        endpoint(endpoint_prefix, STDIN_CHANNEL),
    )
    .map_err(|error| format!("failed to connect ark stdin socket: {error}"))?;

    let welcome = wait_for_message(&iopub, child, log_path, Instant::now() + STARTUP_TIMEOUT)?;
    if !matches!(welcome, Message::Welcome(_)) {
        return Err(format!("expected ark IOPub welcome, received {welcome:?}"));
    }
    let starting = wait_for_message(&iopub, child, log_path, Instant::now() + STARTUP_TIMEOUT)?;
    if !matches!(
        starting,
        Message::Status(ref message)
            if message.content.execution_state == ExecutionState::Starting
    ) {
        return Err(format!(
            "expected ark starting status, received {starting:?}"
        ));
    }

    let frontend = Frontend {
        _context: context,
        session,
        control,
        shell,
        iopub,
        stdin,
    };
    wait_until_ready(&frontend, child, log_path)?;
    Ok(frontend)
}

fn wait_until_ready(
    frontend: &Frontend,
    child: &mut Child,
    log_path: &std::path::Path,
) -> Result<(), String> {
    let message_id = send(&frontend.shell, &frontend.session, KernelInfoRequest {})?;
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let mut replied = false;
    let mut idle = false;

    while !(replied && idle) {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("failed to inspect ark process: {error}"))?
        {
            return Err(format_child_exit(status, log_path));
        }
        if Instant::now() >= deadline {
            return Err(String::from("timed out waiting for ark to initialize"));
        }

        let (iopub_ready, shell_ready) = {
            let mut items = [
                frontend.iopub.socket.as_poll_item(zmq::POLLIN),
                frontend.shell.socket.as_poll_item(zmq::POLLIN),
            ];
            zmq::poll(&mut items, POLL_INTERVAL.as_millis() as i64)
                .map_err(|error| format!("failed to poll ark startup sockets: {error}"))?;
            (items[0].is_readable(), items[1].is_readable())
        };

        if iopub_ready {
            while frontend
                .iopub
                .has_incoming_data()
                .map_err(|error| format!("failed to inspect ark IOPub socket: {error}"))?
            {
                let message = Message::read_from_socket(&frontend.iopub)
                    .map_err(|error| format!("failed to read ark IOPub message: {error}"))?;
                if message
                    .parent_header()
                    .is_some_and(|parent| parent.msg_id == message_id)
                    && matches!(
                        message,
                        Message::Status(ref status)
                            if status.content.execution_state == ExecutionState::Idle
                    )
                {
                    idle = true;
                }
            }
        }

        if shell_ready {
            while frontend
                .shell
                .has_incoming_data()
                .map_err(|error| format!("failed to inspect ark shell socket: {error}"))?
            {
                let message = Message::read_from_socket(&frontend.shell)
                    .map_err(|error| format!("failed to read ark shell message: {error}"))?;
                if message
                    .parent_header()
                    .is_none_or(|parent| parent.msg_id != message_id)
                {
                    continue;
                }
                match message {
                    Message::KernelInfoReply(message) => {
                        verify_kernel_info(&message.content)?;
                        replied = true;
                    }
                    message => {
                        return Err(format!("unexpected ark kernel_info reply: {message:?}"));
                    }
                }
            }
        }
    }

    Ok(())
}

fn wait_for_message(
    socket: &Socket,
    child: &mut Child,
    log_path: &std::path::Path,
    deadline: Instant,
) -> Result<Message, String> {
    loop {
        if socket
            .poll_incoming(POLL_INTERVAL.as_millis() as i64)
            .map_err(|error| format!("failed to poll {} socket: {error}", socket.name))?
        {
            return Message::read_from_socket(socket)
                .map_err(|error| format!("failed to read {} message: {error}", socket.name));
        }
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("failed to inspect ark process: {error}"))?
        {
            return Err(format_child_exit(status, log_path));
        }
        if Instant::now() >= deadline {
            return Err(format!("timed out waiting for ark {}", socket.name));
        }
    }
}

fn send<T: ProtocolMessage>(
    socket: &Socket,
    session: &Session,
    content: T,
) -> Result<String, String> {
    let message = JupyterMessage::create(content, None, session);
    let message_id = message.header.msg_id.clone();
    message
        .send(socket)
        .map_err(|error| format!("failed to send ark {} message: {error}", socket.name))?;
    Ok(message_id)
}

fn endpoint(prefix: &str, channel: u16) -> String {
    format!("ipc://{prefix}:{channel}")
}

fn code_location(
    session_id: &str,
    evaluation_count: u64,
    code: &str,
) -> Result<JupyterPositronLocation, String> {
    let line = code
        .bytes()
        .filter(|byte| *byte == b'\n')
        .count()
        .try_into()
        .map_err(|_| String::from("R cell has too many lines"))?;
    let last_line = if code.ends_with('\n') {
        ""
    } else {
        code.rsplit_once('\n').map_or(code, |(_, line)| line)
    };
    let last_line = last_line.strip_suffix('\r').unwrap_or(last_line);
    let character = last_line
        .len()
        .try_into()
        .map_err(|_| String::from("R cell line is too long"))?;

    Ok(JupyterPositronLocation {
        uri: format!(
            "file:///__mcp-console__/sessions/{session_id}/r/mcp-console-e{evaluation_count:06}.R"
        ),
        range: JupyterPositronRange {
            start: JupyterPositronPosition {
                line: 0,
                character: 0,
            },
            end: JupyterPositronPosition { line, character },
        },
    })
}

fn verify_kernel_info(
    info: &amalthea::wire::kernel_info_full_reply::KernelInfoReply,
) -> Result<(), String> {
    let commit = info
        .language_info
        .positron
        .as_ref()
        .and_then(|positron| positron.commit.as_deref())
        .map(str::trim);
    if info.status == Status::Ok
        && info.implementation == "ark"
        && info.protocol_version == JUPYTER_PROTOCOL_VERSION
        && commit == Some(ark::BUILD_GIT_HASH.trim())
    {
        return Ok(());
    }

    Err(format!(
        "unexpected embedded ark kernel: expected ark protocol {JUPYTER_PROTOCOL_VERSION} at {}, received {} {} protocol {} at {}",
        ark::BUILD_GIT_HASH.trim(),
        info.implementation,
        info.implementation_version,
        info.protocol_version,
        commit.unwrap_or("<unknown>")
    ))
}

fn append_plain_text(output: &mut String, data: &Value) {
    if let Some(text) = data.get("text/plain").and_then(Value::as_str) {
        append_block(output, text);
    }
}

fn append_block(output: &mut String, text: &str) {
    if !output.is_empty() && !output.ends_with('\n') {
        output.push('\n');
    }
    output.push_str(text);
}

fn render_input(mut output: String, prompt: &str) -> String {
    append_block(&mut output, prompt.trim_end());
    append_marker(output, "[input]")
}

fn render_done(output: String) -> String {
    if output.is_empty() {
        String::from("[done]")
    } else {
        output
    }
}

fn append_marker(mut output: String, marker: &str) -> String {
    if !output.is_empty() && !output.ends_with('\n') {
        output.push('\n');
    }
    output.push_str(marker);
    output
}

fn format_child_exit(status: std::process::ExitStatus, log_path: &std::path::Path) -> String {
    let log = fs::read_to_string(log_path).unwrap_or_default();
    let log = log.trim();
    if log.is_empty() {
        format!("ark exited with {status}")
    } else {
        format!("ark exited with {status}:\n{log}")
    }
}
