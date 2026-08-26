use std::io::Write;
use std::process::{Command, Stdio};

use serde_json::{Value, json};

fn exchange(request: Value) -> Value {
    let mut child = Command::new(env!("CARGO_BIN_EXE_mcp-console"))
        .arg("serve")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("mcp-console should start");
    let mut stdin = child.stdin.take().expect("stdin should be piped");
    writeln!(stdin, "{request}").expect("request should be written");
    drop(stdin);

    let output = child.wait_with_output().expect("mcp-console should exit");
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "mcp-console failed: {stderr}");
    let stdout = String::from_utf8(output.stdout).expect("response should be UTF-8");
    let mut lines = stdout.lines();
    let response = lines.next().expect("server should return one response");
    assert!(
        lines.next().is_none(),
        "server returned unexpected extra output: {stdout}"
    );
    serde_json::from_str(response).expect("response should be JSON")
}

#[test]
fn server_supports_codex_legacy_and_modern_protocol_lifecycles() {
    let legacy = exchange(json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "protocol-test",
                "version": "1.0.0"
            }
        }
    }));
    assert_eq!(legacy["result"]["protocolVersion"], "2025-06-18");
    assert!(legacy["result"].get("resultType").is_none());

    let modern = exchange(json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "protocol-test",
                    "version": "1.0.0"
                },
                "io.modelcontextprotocol/clientCapabilities": {}
            }
        }
    }));
    assert_eq!(modern["result"]["resultType"], "complete");
    let supported = modern["result"]["supportedVersions"]
        .as_array()
        .expect("discovery should advertise protocol versions");
    for version in ["2025-06-18", "2025-11-25", "2026-07-28"] {
        assert!(
            supported.iter().any(|candidate| candidate == version),
            "server did not advertise {version}: {supported:?}"
        );
    }
}
