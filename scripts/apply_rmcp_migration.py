from pathlib import Path
from textwrap import dedent


def replace_once(path: str, old: str, new: str) -> None:
    source = Path(path).read_text()
    count = source.count(old)
    if count != 1:
        raise RuntimeError(f"expected one match in {path}, found {count}: {old!r}")
    Path(path).write_text(source.replace(old, new, 1))


replace_once(
    "Cargo.toml",
    'rmcp = { version = "2.2.0",',
    'rmcp = { version = "3.1.4",',
)

replace_once(
    "src/server.rs",
    "use rmcp::{\n    RoleServer, ServerHandler, ServiceExt,",
    "use rmcp::{\n    ErrorData, RoleServer, ServerHandler, ServiceExt,",
)
replace_once(
    "src/server.rs",
    "model::{CallToolRequestParams, CallToolResult, ContentBlock, ErrorData},",
    "model::{CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock},",
)
replace_once(
    "src/server.rs",
    """impl ServerHandler for ConsoleServer {
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        mut context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {""",
    """impl ServerHandler for ConsoleServer {
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        mut context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {""",
)

replace_once(
    "src/transcript.rs",
    """use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, ErrorData, Meta, RequestId,
};""",
    """use rmcp::{
    ErrorData,
    model::{
        CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, RequestId,
        RequestMetaObject,
    },
};""",
)
replace_once(
    "src/transcript.rs",
    "request_meta: &Meta,",
    "request_meta: &RequestMetaObject,",
)
replace_once(
    "src/transcript.rs",
    "pub(crate) fn finish(&self, call: Call, response: &Result<CallToolResult, ErrorData>) {",
    "pub(crate) fn finish(&self, call: Call, response: &Result<CallToolResponse, ErrorData>) {",
)
replace_once(
    "src/transcript.rs",
    "response: &Result<CallToolResult, ErrorData>,",
    "response: &Result<CallToolResponse, ErrorData>,",
)
replace_once(
    "src/transcript.rs",
    """        let event = match response {
            Ok(result) => json!({
                "event": "tool_result",
                "call_id": call_id,
                "result": self.project_result(call_id, result_images, result)?,
            }),
            Err(error) => {""",
    """        let event = match response {
            Ok(CallToolResponse::Complete(result)) => json!({
                "event": "tool_result",
                "call_id": call_id,
                "result": self.project_result(call_id, result_images, result)?,
            }),
            Ok(_) => {
                return Err(
                    "console tool unexpectedly returned a non-final MCP response".to_string(),
                );
            }
            Err(error) => {""",
)
replace_once(
    "src/transcript.rs",
    """        let mut value = serde_json::to_value(result)
            .map_err(|error| format!("failed to serialize tool result: {error}"))?;
        let recorded_content = value
            .get_mut("content")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| "serialized tool result has no content array".to_string())?;""",
    """        let mut value = serde_json::to_value(result)
            .map_err(|error| format!("failed to serialize tool result: {error}"))?;
        let recorded_result = value
            .as_object_mut()
            .ok_or_else(|| "serialized tool result is not an object".to_string())?;
        // The transcript schema records the stable tool payload rather than
        // protocol-version-specific MCP response-envelope fields.
        recorded_result.remove("resultType");
        let recorded_content = recorded_result
            .get_mut("content")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| "serialized tool result has no content array".to_string())?;""",
)

Path("tests/protocol_versions.rs").write_text(
    dedent(
        """\
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
        """
    )
)
