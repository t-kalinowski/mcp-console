#[cfg(target_os = "macos")]
use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(target_os = "macos")]
use std::net::TcpListener;
#[cfg(target_os = "macos")]
use std::os::unix::fs::PermissionsExt;
#[cfg(target_os = "macos")]
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};
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
            .join("target/sandbox-tests")
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
fn version_reports_the_binary_name_and_package_version() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("--version")
        .output()
        .expect("mcp-console should run");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be UTF-8"),
        format!("mcp-console {}\n", env!("CARGO_PKG_VERSION"))
    );
    assert!(output.stderr.is_empty());
}

#[test]
fn stdio_server_registers_only_a_lazy_r_console_tool() {
    let mut client = McpClient::start_without_r(&["serve"]);
    let tools = client.request(2, "tools/list", None);
    let tools = tools["result"]["tools"]
        .as_array()
        .expect("tools should be an array");

    assert_eq!(tools.len(), 1);
    assert_eq!(tools[0]["name"], "console");
    assert_eq!(tools[0]["inputSchema"]["type"], "object");
    assert_eq!(tools[0]["inputSchema"]["additionalProperties"], false);
    assert!(
        tools[0]["inputSchema"]["required"].is_null()
            || tools[0]["inputSchema"]["required"] == json!([])
    );
    assert_eq!(
        tools[0]["inputSchema"]["properties"]
            .as_object()
            .expect("console properties should be an object")
            .keys()
            .collect::<Vec<_>>(),
        ["r", "stdin"]
    );

    assert_eq!(
        client.call_console_error(3, json!({})),
        "send exactly one of r or stdin"
    );
    assert_eq!(
        client.call_console_error(4, json!({"r": "1", "stdin": "\n"})),
        "send exactly one of r or stdin"
    );
    assert_eq!(
        client.call_console_error(5, json!({"stdin": "\n"})),
        "stdin is accepted only at an R input prompt"
    );

    let stopped = client.call_console_error(6, json!({"r": "1"}));
    assert!(
        stopped.starts_with("[stopped:"),
        "startup failure should be an explicit stopped state: {stopped:?}"
    );
    assert_eq!(
        client.call_console_error(7, json!({"r": "1"})),
        stopped,
        "a stopped worker must not restart implicitly"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_runs_complete_top_level_r_cells_in_persistent_state() {
    let mut client = McpClient::start(&["serve"]);
    let code = r#"
answer <- (
    38 + 2
)
answer + 2
cat("done\n")
invisible(99)
cores <- parallel::detectCores()
"parallel" %in% names(getLoadedDLLs())
"#;

    assert_eq!(
        client.call_console(2, json!({"r": code})),
        "[1] 42\ndone\n[1] TRUE\n"
    );
    assert_eq!(
        client.call_console(3, json!({"r": "silent <- 1"})),
        "[done]"
    );
    assert_eq!(
        client.call_console(4, json!({"r": "1\n2"})),
        "[1] 1\n[1] 2\n"
    );
    assert_eq!(client.call_console(5, json!({"r": "answer"})), "[1] 40\n");

    let calls = client.call_console(
        6,
        json!({"r": r#"
user_calls <- function() {
    vapply(sys.calls(), deparse1, character(1))
}
user_calls()
"#}),
    );
    assert!(calls.contains("\"user_calls()\""), "R calls: {calls:?}");
    assert!(!calls.contains("mcp_console"), "R calls: {calls:?}");
    assert!(!calls.contains("base::get"), "R calls: {calls:?}");

    assert_eq!(
        client.call_console(
            7,
            json!({"r": r#"
invisible(addTaskCallback(local({
    first <- TRUE
    function(expr, ...) {
        if (first) {
            first <<- FALSE
            return(TRUE)
        }
        cat(deparse1(expr), "\n", sep = "")
        FALSE
    }
}),
    name = "mcp-console-test"
))
mcp_console_callback_probe <- 42
"#}),
        ),
        "mcp_console_callback_probe <- 42\n"
    );

    assert_eq!(
        client.call_console(
            8,
            json!({"r": r#"
warning("careful")
invisible(42)
identical(base::.Last.value, 42) &&
    !exists(".Last.value", envir = globalenv(), inherits = FALSE)
"#}),
        ),
        "Warning message:\ncareful \n[1] TRUE\n"
    );

    assert_eq!(
        client.call_console(
            9,
            json!({"r": r#"
job <- parallel::mcparallel(cat("forked output\n"))
invisible(parallel::mccollect(job))
"#}),
        ),
        "[done]"
    );

    assert_eq!(
        client.call_console(
            10,
            json!({"r": r#"
..mcp_console_value.. <- 42
..mcp_console_value..
"#}),
        ),
        "[1] 42\n"
    );

    let long_value = "é".repeat(3000);
    let long_line = format!(
        r#"
long_line_value <- "{long_value}"
nchar(long_line_value)
"#
    );
    assert_eq!(
        client.call_console(11, json!({"r": long_line})),
        "[1] 3000\n"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_discovers_r_inside_the_worker_sandbox() {
    let test_directory = TestDirectory::new("native-worker-r-discovery");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    let escaped = test_directory.path().join("escaped.txt");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
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
fn stdio_console_shutdown_is_bounded_during_r_discovery() {
    let test_directory = TestDirectory::new("native-worker-r-discovery-shutdown");
    let fake_bin = test_directory.path().join("bin");
    let fake_r = fake_bin.join("R");
    fs::create_dir(&fake_bin).expect("fake bin directory should be created");
    fs::write(
        &fake_r,
        r#"#!/bin/sh
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

    let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
    command.arg("serve").env_remove("R_HOME").env("PATH", path);
    let mut client = McpClient::spawn(command);
    client.send_console(2, json!({"r": "1 + 1"}));

    let elapsed = client.close_within(Duration::from_secs(2));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_treats_r_failures_as_recoverable_language_outcomes() {
    let mut client = McpClient::start(&["serve"]);
    assert_eq!(
        client.call_console(2, json!({"r": "answer <- 40"})),
        "[done]"
    );

    let incomplete = client.call_console_response(
        3,
        json!({"r": r#"
answer <- 41
answer + (
"#}),
    );
    assert_eq!(incomplete["result"]["isError"], false);
    assert_eq!(
        incomplete["result"]["content"][0]["text"],
        "Error: Incomplete code\n"
    );

    let syntax_error = client.call_console_response(
        4,
        json!({"r": r#"
answer <- 42
)
"#}),
    );
    assert_eq!(syntax_error["result"]["isError"], false);
    assert!(
        syntax_error["result"]["content"][0]["text"]
            .as_str()
            .expect("R syntax error should be text")
            .contains("unexpected ')'"),
        "syntax error: {:?}",
        syntax_error["result"]["content"][0]["text"]
    );
    assert_eq!(client.call_console(5, json!({"r": "answer"})), "[1] 42\n");

    let stopped = client.call_console_response(
        6,
        json!({"r": r#"
cat("before\n")
stop("boom")
"#}),
    );
    assert_eq!(stopped["result"]["isError"], false);
    assert_eq!(
        stopped["result"]["content"][0]["text"],
        "before\nError: boom\n"
    );
    assert_eq!(client.call_console(7, json!({"r": "answer"})), "[1] 42\n");

    let nested = client.call_console_response(
        8,
        json!({"r": r#"
g <- function() stop("boom")
f <- function() g()
f()
"#}),
    );
    assert_eq!(nested["result"]["isError"], false);
    assert!(
        nested["result"]["content"][0]["text"]
            .as_str()
            .expect("R error should be text")
            .contains("boom")
    );

    let traceback = client.call_console(9, json!({"r": "traceback()"}));
    assert!(
        traceback.contains("stop(\"boom\")"),
        "traceback: {traceback:?}"
    );
    assert!(traceback.contains("g()"), "traceback: {traceback:?}");
    assert!(traceback.contains("f()"), "traceback: {traceback:?}");
    assert!(
        !traceback.contains("mcp_console"),
        "traceback: {traceback:?}"
    );
    assert!(!traceback.contains("base::get"), "traceback: {traceback:?}");

    let print_error = client.call_console_response(
        10,
        json!({"r": r#"
print.mcp_console_boom <- function(...) stop("print failed")
structure(1, class = "mcp_console_boom")
"#}),
    );
    assert_eq!(print_error["result"]["isError"], false);
    assert_eq!(
        print_error["result"]["content"][0]["text"],
        "Error in print.mcp_console_boom(x) : print failed\n"
    );
    assert_eq!(client.call_console(11, json!({"r": "answer"})), "[1] 42\n");
}

#[cfg(target_os = "macos")]
#[test]
fn stdio_console_supplies_exact_stdin_to_readline_and_browser() {
    let mut client = McpClient::start(&["serve"]);
    assert_eq!(
        client.call_console(
            2,
            json!({"r": r#"
name <- readline("name> ")
paste("hello", name)
"#}),
        ),
        "name>\n[input]"
    );
    assert_eq!(
        client.call_console(3, json!({"stdin": "Ad"})),
        "name>\n[input]"
    );
    assert_eq!(
        client.call_console(4, json!({"stdin": "a\n"})),
        "[1] \"hello Ada\"\n"
    );
    assert_eq!(
        client.call_console_error(5, json!({"stdin": "unused\n"})),
        "stdin is accepted only at an R input prompt"
    );

    assert_eq!(
        client.call_console(
            6,
            json!({"r": r#"
first <- readline("first> ")
second <- readline("second> ")
paste(first, second)
"#}),
        ),
        "first>\n[input]"
    );
    assert_eq!(
        client.call_console(7, json!({"stdin": "one\ntwo\nunused\n"})),
        "[1] \"one two\"\n"
    );
    assert_eq!(
        client.call_console(
            8,
            json!({"r": r#"
fresh <- readline("fresh> ")
fresh
"#}),
        ),
        "fresh>\n[input]"
    );
    assert_eq!(
        client.call_console(9, json!({"stdin": "kept\n"})),
        "[1] \"kept\"\n"
    );

    assert_eq!(
        client.call_console(
            10,
            json!({"r": r#"
readline("fail> ")
stop("boom")
"#}),
        ),
        "fail>\n[input]"
    );
    assert_eq!(
        client.call_console(11, json!({"stdin": "used\nstale\n"})),
        "[1] \"used\"\nError: boom\n"
    );
    assert_eq!(
        client.call_console(
            12,
            json!({"r": r#"
fresh <- readline("after error> ")
fresh
"#}),
        ),
        "after error>\n[input]"
    );
    assert_eq!(
        client.call_console(13, json!({"stdin": "new\n"})),
        "[1] \"new\"\n"
    );

    let browser = client.call_console(14, json!({"r": "browser()"}));
    assert!(
        browser.starts_with("Called from: top level"),
        "browser output: {browser:?}"
    );
    assert!(browser.contains("\nBrowse["), "browser output: {browser:?}");
    assert!(browser.ends_with(">\n[input]"));
    assert_eq!(
        client.call_console_error(15, json!({"r": "1"})),
        "cannot evaluate R code while the session is waiting for stdin"
    );

    let browser_eval = client.call_console(16, json!({"stdin": "1 + 1\n"}));
    assert!(
        browser_eval.starts_with("[1] 2\nBrowse["),
        "browser output: {browser_eval:?}"
    );
    assert!(browser_eval.ends_with(">\n[input]"));
    assert_eq!(client.call_console(17, json!({"stdin": "c\n"})), "[done]");
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
fn stdio_console_keeps_a_stopped_worker_stopped() {
    let mut client = McpClient::start(&["serve"]);
    let stopped = client.call_console_error(
        2,
        json!({"r": r#"
quit(save = "no", status = 23, runLast = FALSE)
"#}),
    );
    assert!(stopped.starts_with("[stopped:"), "worker exit: {stopped:?}");
    assert!(stopped.contains("23"), "worker exit: {stopped:?}");
    assert_eq!(
        client.call_console_error(3, json!({"r": "1"})),
        stopped,
        "a stopped worker must not restart implicitly"
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
        "value>\n[input]"
    );
    client.send_console(3, json!({"stdin": "resume\n"}));

    let elapsed = client.close_within(Duration::from_secs(2));
    assert!(
        elapsed < Duration::from_secs(2),
        "server shutdown took {elapsed:?}"
    );
}

#[cfg(not(target_os = "macos"))]
#[test]
fn stdio_console_does_not_start_an_unsandboxed_r_session() {
    let mut client = McpClient::start(&["serve"]);

    assert_eq!(
        client.call_console_error(2, json!({"r": "1 + 1"})),
        "[stopped: sandboxed R sessions are not supported on this operating system]"
    );
}

struct McpClient {
    server: Child,
    input: Option<std::process::ChildStdin>,
    output: BufReader<std::process::ChildStdout>,
    closed: bool,
}

impl McpClient {
    fn start(arguments: &[&str]) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
        command.args(arguments);
        Self::spawn(command)
    }

    fn start_without_r(arguments: &[&str]) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_mcp-console"));
        command
            .args(arguments)
            .env("R_HOME", "/mcp-console-test/missing-r")
            .env("PATH", "");
        Self::spawn(command)
    }

    fn spawn(mut command: Command) -> Self {
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
                "name": "console",
                "arguments": arguments
            })),
        )
    }

    fn send_console(&mut self, id: u64, arguments: Value) {
        write_message(
            self.input.as_mut().expect("stdin should be open"),
            &json!({
                "jsonrpc": "2.0",
                "id": id,
                "method": "tools/call",
                "params": {
                    "name": "console",
                    "arguments": arguments
                }
            }),
        );
    }

    fn call_console(&mut self, id: u64, arguments: Value) -> String {
        let response = self.call_console_response(id, arguments);
        assert_eq!(response["result"]["isError"], false, "{response}");
        response_text(&response)
    }

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
                panic!("mcp-console did not stop within {timeout:?}");
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
fn sandbox_preserves_python_arguments_and_standard_output() {
    let script = r#"
import sys

print("|".join(sys.argv[1:]))
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args([
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "hello world",
            "$(not-a-command)",
            "--child-option",
        ])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(
        String::from_utf8(output.stdout).expect("stdout should be UTF-8"),
        "hello world|$(not-a-command)|--child-option\n"
    );
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_read_only_policy_allows_python_multiprocessing_semaphores() {
    let script = r#"
import multiprocessing as mp
import operator

context = mp.get_context("spawn")
lock = context.Lock()
lock.acquire()
child = context.Process(target=operator.methodcaller("release"), args=(lock,))
child.start()
child.join()
assert child.exitcode == 0
assert lock.acquire(timeout=1)
print("semaphore shared")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed Python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"semaphore shared\n");
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_does_not_require_home() {
    let script = r#"
print("ran")
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .env_remove("HOME")
        .args(["sandbox", "--", "python", "-c", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(output.status.success());
    assert_eq!(output.stdout, b"ran\n");
    assert!(output.stderr.is_empty());
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_supports_r_runtime_queries_and_temporary_writes() {
    let script = r#"
stopifnot(parallel::detectCores() >= 1)

output <- file.path(tempdir(), "result.txt")
writeLines("sandboxed R", output)
writeLines(readLines(output))
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed R failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed R\n");
}

#[cfg(target_os = "macos")]
#[test]
fn sandbox_read_only_policy_allows_processx_pty_processes() {
    let script = r#"
p <- processx::process$new("/bin/cat", pty = TRUE)
on.exit(if (p$is_alive()) p$kill())
p$write_input("sandboxed pty\n")
stopifnot(p$poll_io(5000)[["output"]] == "ready")
cat(p$read_output())
invisible(p$kill())
"#;
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(["sandbox", "--", "Rscript", "-e", script])
        .output()
        .expect("mcp-console sandbox should run");

    assert!(
        output.status.success(),
        "sandboxed processx failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(output.stdout, b"sandboxed pty\r\n");
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
    let test_directory = TestDirectory::new("write-boundary");
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
