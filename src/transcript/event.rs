use rmcp::{
    ErrorData,
    model::{Annotations, CallToolRequestParams, ContentBlock, MetaObject, RequestId, TextContent},
};
use serde::Serialize;
use serde_json::Value;

#[derive(Serialize)]
pub(super) struct Envelope<'a> {
    #[serde(flatten)]
    pub event: Event<'a>,
    pub schema_version: u64,
    pub run_id: &'a str,
    pub sequence: u64,
    pub at: String,
}

#[derive(Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub(super) enum Event<'a> {
    SessionStarted {
        session: &'a str,
        working_directory: &'a str,
        dynamic_resolution: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        target: Option<&'a Value>,
    },
    TargetGeneration {
        container_id: &'a str,
    },
    ToolCall {
        call_id: u64,
        request_id: &'a RequestId,
        request: &'a CallToolRequestParams,
    },
    ArtifactCreated {
        artifact_id: u64,
        call_id: u64,
        path: &'a str,
        mime_type: &'a str,
        bytes: usize,
    },
    CellOutput {
        call_id: u64,
        path: &'a str,
        retained_bytes: u64,
        inline_omitted_bytes: u64,
        discarded_bytes: u64,
        retention_limit_bytes: u64,
    },
    ToolResult {
        call_id: u64,
        #[serde(flatten)]
        outcome: Outcome<'a>,
    },
}

#[derive(Serialize)]
#[serde(untagged)]
pub(super) enum Outcome<'a> {
    Result { result: RecordedResult<'a> },
    Error { error: &'a ErrorData },
}

// Record the stable tool payload, without protocol-version-specific resultType.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub(super) struct RecordedResult<'a> {
    pub content: Vec<RecordedContent<'a>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub structured_content: Option<&'a Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
    #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
    pub meta: Option<&'a MetaObject>,
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub(super) enum RecordedContent<'a> {
    Text(&'a TextContent),
    Image {
        #[serde(rename = "mimeType")]
        mime_type: &'a str,
        #[serde(rename = "_meta", skip_serializing_if = "Option::is_none")]
        meta: Option<&'a MetaObject>,
        #[serde(skip_serializing_if = "Option::is_none")]
        annotations: Option<&'a Annotations>,
        #[serde(rename = "artifactId")]
        artifact_id: u64,
        path: String,
    },
    #[serde(untagged)]
    Other(&'a ContentBlock),
}
