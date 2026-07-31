#[cfg(target_os = "macos")]
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(target_os = "macos")]
use std::net::TcpListener;
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
#[cfg(target_os = "macos")]
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

#[cfg(target_os = "macos")]
struct TestDirectory(PathBuf);

#[cfg(target_os = "macos")]
impl TestDirectory {
    fn new(name: &str) -> Self {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock should be after the Unix epoch")
            .as_nanos();
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("target/r-console-tests")
            .join(format!("{name}-{}-{unique}", std::process::id()));
        fs::create_dir_all(&path).expect("test directory should be created");
        Self(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

#[cfg(target_os = "macos")]
impl Drop for TestDirectory {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("test directory should be removed");
    }
}

#[test]
fn stdio_server_registers_only_an_r_send_tool() {
    let mut client = McpClient::start();
    let tools = client.request(2, "tools/list", None);
    let tools = tools["result"]["tools"]
        .as_array()
        .expect("tools should be an array");

    assert_eq!(tools.len(), 1);
    assert_eq!(tools[0]["name"], "send");
    assert_eq!(tools[0]["inputSchema"]["type"], "object");
    assert_eq!(tools[0]["inputSchema"]["additionalProperties"], false);
    assert_eq!(
        tools[0]["inputSchema"]["properties"]
            .as_object()
            .expect("send properties should be an object")
            .keys()
            .collect::<Vec<_>>(),
        ["r", "stdin"]
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_runs_persistent_r_with_readline_and_browser_input() {
    let mut client = McpClient::start();
    let assignment = r#"
answer <- 40
answer + 2
"#;

    assert_eq!(client.call_send(2, json!({"r": assignment})), "[1] 42");
    assert_eq!(client.call_send(3, json!({"r": "answer + 3"})), "[1] 43");

    let readline = r#"
name <- readline("name> ")
paste("hello", name)
"#;
    assert_eq!(
        client.call_send(4, json!({"r": readline})),
        "name>\n[input]"
    );
    assert_eq!(
        client.call_send(5, json!({"stdin": "Ada\n"})),
        "[1] \"hello Ada\""
    );

    let browser = client.call_send(6, json!({"r": "browser()"}));
    assert!(
        browser.starts_with("Browse["),
        "browser output: {browser:?}"
    );
    assert!(browser.ends_with(">\n[input]"));

    let browser_eval = client.call_send(7, json!({"stdin": "1 + 1\n"}));
    assert!(
        browser_eval.starts_with("[1] 2\nBrowse["),
        "browser output: {browser_eval:?}"
    );
    assert!(browser_eval.ends_with(">\n[input]"));

    assert_eq!(client.call_send(8, json!({"stdin": "c\n"})), "[done]");

    let error = client.call_send(9, json!({"r": "stop(\"boom\")"}));
    assert!(error.contains("boom"), "R error output: {error:?}");
    assert!(!error.ends_with("\n[done]"));
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_preserves_complete_cell_console_semantics() {
    let mut client = McpClient::start();

    let visible = r#"
1
2
"#;
    assert_eq!(client.call_send(2, json!({"r": visible})), "[1] 1\n[1] 2");

    let mixed = r#"
1
invisible(2)
3
"#;
    assert_eq!(client.call_send(3, json!({"r": mixed})), "[1] 1\n[1] 3");

    let explicit = r#"
cat("before\n")
4
"#;
    assert_eq!(client.call_send(4, json!({"r": explicit})), "before\n[1] 4");

    let later_error = r#"
retained_before_error <- 41
stop("later error")
retained_before_error <- 0
"#;
    let error = client.call_send(5, json!({"r": later_error}));
    assert!(error.contains("later error"), "R error output: {error:?}");
    assert!(!error.ends_with("\n[done]"));
    assert_eq!(
        client.call_send(6, json!({"r": "retained_before_error + 1"})),
        "[1] 42"
    );

    assert_eq!(client.call_send(7, json!({"r": "invisible(99)"})), "[done]");
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_uses_a_short_private_ipc_root() {
    let test_directory = TestDirectory::new("deep-tmpdir");
    let mut deep_tmpdir = test_directory.path().to_path_buf();
    for _ in 0..6 {
        deep_tmpdir.push("directory-name-long-enough-to-overflow-a-unix-socket");
    }
    fs::create_dir_all(&deep_tmpdir).expect("deep temporary directory should be created");

    let mut client = McpClient::start_with_tmpdir(&deep_tmpdir);
    assert_eq!(client.call_send(2, json!({"r": "1 + 1"})), "[1] 2");
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_attributes_r_source_and_keeps_user_call_stacks_clean() {
    let mut client = McpClient::start();
    let source = r#"
f <- function() {
    NULL
}
source_reference <- getSrcref(f)
attr(source_reference, "srcfile")$filename
"#;
    let filename = client.call_send(2, json!({"r": source}));
    assert!(
        filename.starts_with(r#"[1] "file:///__mcp-console__/sessions/"#),
        "source filename: {filename:?}"
    );
    assert!(
        filename.ends_with(r#"/r/mcp-console-e000001.R""#),
        "source filename: {filename:?}"
    );

    let stack = r#"
stack_probe <- function() sys.calls()
stack_probe()
"#;
    let stack = client.call_send(3, json!({"r": stack}));
    assert!(
        stack.starts_with("[[1]]\nstack_probe()"),
        "R call stack: {stack:?}"
    );
    assert!(!stack.contains("mcp_console"), "R call stack: {stack:?}");

    let error = r#"
source_error <- function() {
    stop("source identity failure")
}
source_error()
"#;
    let error = client.call_send(4, json!({"r": error}));
    assert!(
        error.contains("source identity failure"),
        "R error output: {error:?}"
    );
    assert!(
        error.contains("mcp-console-e000003.R:5:1"),
        "R error output: {error:?}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_buffers_exact_stdin_for_only_the_active_evaluation() {
    let mut client = McpClient::start();

    assert_eq!(
        client.call_send_error(2, json!({"stdin": "unused\n"})),
        "stdin is accepted only at an R input prompt"
    );

    let one_line = r#"
value <- readline("value> ")
value
"#;
    assert_eq!(
        client.call_send(3, json!({"r": one_line})),
        "value>\n[input]"
    );
    assert_eq!(client.call_send(4, json!({"stdin": ""})), "value>\n[input]");
    assert_eq!(
        client.call_send_error(5, json!({"r": "1 + 1"})),
        "cannot evaluate R code while the session is waiting for stdin"
    );
    assert_eq!(
        client.call_send(6, json!({"stdin": "partial"})),
        "value>\n[input]"
    );
    assert_eq!(
        client.call_send(7, json!({"stdin": "\n"})),
        r#"[1] "partial""#
    );

    let two_lines = r#"
first <- readline("first> ")
second <- readline("second> ")
paste(first, second)
"#;
    assert_eq!(
        client.call_send(8, json!({"r": two_lines})),
        "first>\n[input]"
    );
    assert_eq!(
        client.call_send(9, json!({"stdin": "one\ntwo\n"})),
        r#"[1] "one two""#
    );

    assert_eq!(
        client.call_send(10, json!({"r": one_line})),
        "value>\n[input]"
    );
    assert_eq!(
        client.call_send(11, json!({"stdin": "used\nleftover\n"})),
        r#"[1] "used""#
    );
    assert_eq!(
        client.call_send(12, json!({"r": one_line})),
        "value>\n[input]"
    );
    assert_eq!(
        client.call_send(13, json!({"stdin": ""})),
        "value>\n[input]"
    );
    assert_eq!(client.call_send(14, json!({"stdin": "\n"})), r#"[1] """#);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_routes_menu_input_through_stdin() {
    let mut client = McpClient::start();
    let menu = r#"
choice <- menu(c("first", "second"), title = "Choose")
choice
"#;
    let prompt = client.call_send(2, json!({"r": menu}));
    assert!(prompt.contains("Choose"), "menu output: {prompt:?}");
    assert!(
        prompt.ends_with("Selection:\n[input]"),
        "menu output: {prompt:?}"
    );
    assert_eq!(client.call_send(3, json!({"stdin": "2\n"})), "[1] 2");
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_routes_recover_through_stdin() {
    let mut client = McpClient::start();
    let recover = r#"
inner <- function() stop("recover failure")
outer <- function() inner()
options(error = recover)
outer()
"#;

    let prompt = client.call_send(2, json!({"r": recover}));
    assert!(prompt.starts_with("Browse["), "recover output: {prompt:?}");
    assert!(prompt.ends_with(">\n[input]"), "recover output: {prompt:?}");

    let stack = client.call_send(3, json!({"stdin": "where\n"}));
    assert!(stack.contains("inner()"), "recover stack: {stack:?}");
    assert!(stack.contains("outer()"), "recover stack: {stack:?}");
    assert!(stack.ends_with(">\n[input]"), "recover stack: {stack:?}");

    let error = client.call_send(4, json!({"stdin": "Q\n"}));
    assert!(
        error.contains("recover failure"),
        "recover error: {error:?}"
    );
    assert!(error.contains("1. outer()"), "recover error: {error:?}");
    assert!(error.contains("2. inner()"), "recover error: {error:?}");
    assert!(!error.ends_with("\n[done]"));
    assert_eq!(
        client.call_send(5, json!({"r": "options(error = NULL)"})),
        "[done]"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_retains_a_stopped_worker_diagnostic() {
    let mut client = McpClient::start();
    let quit = r#"
q("no", status = 17, runLast = FALSE)
"#;
    let stopped = client.call_send_error(2, json!({"r": quit}));
    assert!(stopped.starts_with("[stopped: ark exited with"));
    assert_eq!(client.call_send_error(3, json!({"r": "1 + 1"})), stopped);
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_send_sandboxes_ark_filesystem_and_network_access() {
    let test_directory = TestDirectory::new("ark-boundary");
    let host_path = test_directory.path().join("host.txt");
    fs::write(&host_path, "host data").expect("host fixture should be created");
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

host_write <- tryCatch({{
    suppressWarnings(writeLines("changed", {host_file}))
    "allowed"
}}, error = function(error) "blocked")

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

cat(readLines(temporary_file), "|", host_write, "|", network, "\n", sep = "")
"#
    );
    let mut client = McpClient::start();

    assert_eq!(
        client.call_send(2, json!({"r": code})),
        "temporary|blocked|blocked\n"
    );
    assert_eq!(
        fs::read_to_string(host_path).expect("host fixture should remain readable"),
        "host data"
    );
}

#[cfg(not(target_os = "macos"))]
#[test]
fn stdio_send_does_not_start_an_unsandboxed_r_session() {
    let mut client = McpClient::start();
    let response = client.request(
        2,
        "tools/call",
        Some(json!({
            "name": "send",
            "arguments": {
                "r": "1 + 1"
            }
        })),
    );

    assert_eq!(response["result"]["isError"], true);
    assert_eq!(
        response["result"]["content"][0]["text"],
        "sandboxed R sessions are not supported on this operating system"
    );
}

struct McpClient {
    server: Child,
    input: Option<std::process::ChildStdin>,
    output: BufReader<std::process::ChildStdout>,
}

impl McpClient {
    fn start() -> Self {
        Self::start_command(Command::new(env!("CARGO_BIN_EXE_mcp-console")))
    }

    #[cfg(target_os = "macos")]
    fn start_with_tmpdir(tmpdir: &Path) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
        command.env("TMPDIR", tmpdir);
        Self::start_command(command)
    }

    fn start_command(mut command: Command) -> Self {
        let mut server = command
            .arg("serve")
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

    #[cfg(target_os = "macos")]
    fn call_send(&mut self, id: u64, arguments: Value) -> String {
        let response = self.call_send_response(id, arguments);
        assert_eq!(
            response["result"]["isError"], false,
            "send response: {response}"
        );
        response["result"]["content"][0]["text"]
            .as_str()
            .expect("send output should be text")
            .to_owned()
    }

    #[cfg(target_os = "macos")]
    fn call_send_error(&mut self, id: u64, arguments: Value) -> String {
        let response = self.call_send_response(id, arguments);
        assert_eq!(
            response["result"]["isError"], true,
            "send response: {response}"
        );
        response["result"]["content"][0]["text"]
            .as_str()
            .expect("send error should be text")
            .to_owned()
    }

    #[cfg(target_os = "macos")]
    fn call_send_response(&mut self, id: u64, arguments: Value) -> Value {
        let response = self.request(
            id,
            "tools/call",
            Some(json!({
                "name": "send",
                "arguments": arguments
            })),
        );
        assert_eq!(response["result"]["content"][0]["type"], "text");
        response
    }
}

impl Drop for McpClient {
    fn drop(&mut self) {
        drop(self.input.take());
        let status = self
            .server
            .wait()
            .expect("mcp-console should stop when stdin closes");
        let mut stderr = Vec::new();
        self.server
            .stderr
            .take()
            .expect("stderr should be piped")
            .read_to_end(&mut stderr)
            .expect("stderr should be readable");
        assert!(
            status.success(),
            "server exited with {status}; stderr: {}",
            String::from_utf8_lossy(&stderr)
        );
        assert!(
            stderr.is_empty(),
            "server stderr: {}",
            String::from_utf8_lossy(&stderr)
        );
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
