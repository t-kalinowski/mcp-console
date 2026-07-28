use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use serde_json::{Value, json};

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
fn stdio_server_registers_only_an_echoing_console_tool() {
    assert_stdio_server(&[]);
    assert_stdio_server(&["serve"]);
}

fn assert_stdio_server(arguments: &[&str]) {
    let mut server = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .args(arguments)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("mcp-console should start");
    let mut server_input = server.stdin.take().expect("stdin should be piped");
    let mut server_output = BufReader::new(server.stdout.take().expect("stdout should be piped"));

    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {
                    "name": "acceptance-test",
                    "version": "1.0.0"
                }
            }
        }),
    );
    let initialize = read_message(&mut server_output);
    assert_eq!(initialize["jsonrpc"], "2.0");
    assert_eq!(initialize["id"], 1);
    assert_eq!(initialize["result"]["protocolVersion"], "2025-11-25");
    assert_eq!(initialize["result"]["capabilities"], json!({"tools": {}}));
    assert_eq!(initialize["result"]["serverInfo"]["name"], "mcp-console");
    assert_eq!(
        initialize["result"]["serverInfo"]["version"],
        env!("CARGO_PKG_VERSION")
    );

    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }),
    );
    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list"
        }),
    );
    let tools = read_message(&mut server_output);
    assert_eq!(tools["jsonrpc"], "2.0");
    assert_eq!(tools["id"], 2);
    assert_eq!(
        tools["result"]["tools"]
            .as_array()
            .expect("tools should be an array")
            .len(),
        1
    );
    assert_eq!(tools["result"]["tools"][0]["name"], "console");
    assert_eq!(tools["result"]["tools"][0]["inputSchema"]["type"], "object");

    let arguments = json!({
        "python": "print('hello')",
        "wait_ms": 0
    });
    write_message(
        &mut server_input,
        &json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "console",
                "arguments": arguments
            }
        }),
    );
    let call = read_message(&mut server_output);
    assert_eq!(call["jsonrpc"], "2.0");
    assert_eq!(call["id"], 3);
    assert_eq!(call["result"]["content"][0]["type"], "text");
    assert_eq!(
        serde_json::from_str::<Value>(
            call["result"]["content"][0]["text"]
                .as_str()
                .expect("console output should be text")
        )
        .expect("console output should be JSON"),
        arguments
    );

    drop(server_input);
    drop(server_output);
    let output = server
        .wait_with_output()
        .expect("mcp-console should stop when stdin closes");
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
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

#[test]
fn sandbox_requires_a_command() {
    let output = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("sandbox")
        .output()
        .expect("mcp-console should run");

    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(
        String::from_utf8(output.stderr).expect("stderr should be UTF-8"),
        "usage: mcp-console sandbox [--] COMMAND [ARG]...\n"
    );
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
