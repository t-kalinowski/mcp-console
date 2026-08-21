#![cfg(target_os = "macos")]

use std::io::{BufRead as _, BufReader, Read as _, Write as _};
use std::os::unix::process::CommandExt as _;
use std::path::PathBuf;
use std::process::{Command, Stdio};

use base64::Engine as _;
use serde_json::{Value, json};

const ITERATIONS: usize = 2_500;
const BURSTS_PER_ITERATION: usize = 4;

#[test]
fn coalesces_immediately_available_raw_output_without_losing_bytes() {
    let binary = env!("CARGO_BIN_EXE_mcp-console");
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/raw_output_burst_worker");
    let mut command = Command::new(binary);
    command
        .args(["worker-relay", "python3"])
        .arg(fixture)
        .env("MCP_CONSOLE_BURST_ITERATIONS", ITERATIONS.to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.process_group(0);
    let mut relay = command.spawn().expect("worker relay should start");
    let mut stdin = relay.stdin.take().expect("relay stdin should be piped");
    let mut stdout = BufReader::new(relay.stdout.take().expect("relay stdout should be piped"));

    let mut line = String::new();
    stdout
        .read_line(&mut line)
        .expect("relay ready event should be readable");
    assert_eq!(
        serde_json::from_str::<Value>(&line).unwrap(),
        json!({"kind": "ready"})
    );
    stdin
        .write_all(b"{\"kind\":\"evaluate\",\"language\":\"r\",\"source\":\"burst\"}\n")
        .expect("evaluation command should be writable");
    stdin.flush().expect("evaluation command should flush");

    let mut streams = [Vec::new(), Vec::new()];
    let mut output_wire_bytes = 0;
    loop {
        line.clear();
        if stdout
            .read_line(&mut line)
            .expect("relay event should be readable")
            == 0
        {
            break;
        }
        let event: Value = serde_json::from_str(&line).expect("relay event should be JSON");
        let kind = event["kind"]
            .as_str()
            .expect("relay event should have a kind");
        let Some((stream, encoded)) = (match kind {
            "stdout" => Some((0, false)),
            "stderr" => Some((1, false)),
            "stdout_bytes" => Some((0, true)),
            "stderr_bytes" => Some((1, true)),
            _ => None,
        }) else {
            continue;
        };
        let data = event["data"]
            .as_str()
            .expect("output event should have data");
        if encoded {
            streams[stream].extend(
                base64::engine::general_purpose::STANDARD
                    .decode(data)
                    .expect("byte output should be base64"),
            );
        } else {
            streams[stream].extend(data.as_bytes());
        }
        output_wire_bytes += line.len();
    }

    let status = relay.wait().expect("worker relay should be waitable");
    let mut stderr = String::new();
    relay
        .stderr
        .take()
        .expect("relay stderr should be piped")
        .read_to_string(&mut stderr)
        .expect("relay stderr should be readable");
    assert!(status.success(), "relay failed: {stderr}");
    assert!(stderr.is_empty(), "relay stderr: {stderr}");

    let stdout_iteration = b"ono-newline\rcaf\xc3\xa9\xf0\x9f\x91\xa9\xff\n";
    let stderr_iteration = b"eno-newline\rna\xc3\xafve\xf0\x9f\x92\xa5\xfe\n";
    assert_eq!(
        streams[0],
        stdout_iteration.repeat(ITERATIONS * BURSTS_PER_ITERATION)
    );
    assert_eq!(
        streams[1],
        stderr_iteration.repeat(ITERATIONS * BURSTS_PER_ITERATION)
    );
    let raw_bytes = streams.iter().map(Vec::len).sum::<usize>();
    assert!(
        output_wire_bytes * 2 < raw_bytes * 5,
        "relay output used {output_wire_bytes} JSONL bytes for {raw_bytes} raw bytes"
    );
}
