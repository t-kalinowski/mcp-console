use std::fs::{DirBuilder, File, OpenOptions};
use std::io::{BufWriter, Write};
#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt as _, OpenOptionsExt as _};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use base64::Engine as _;
use chrono::{DateTime, SecondsFormat, Utc};
use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, ErrorData, Meta, RequestId,
};
use serde_json::{Value, json};

const SCHEMA_VERSION: u64 = 1;

#[derive(Clone)]
pub(crate) struct Transcript(Arc<Mutex<TranscriptState>>);

struct TranscriptState {
    working_directory: PathBuf,
    active: Option<ActiveTranscript>,
    failure: Option<String>,
}

struct ActiveTranscript {
    directory: PathBuf,
    writer: BufWriter<File>,
    run_id: String,
    sequence: u64,
    next_call_id: u64,
    next_artifact_id: u64,
}

#[derive(Clone)]
pub(crate) struct Call {
    id: u64,
    result_images: Arc<Mutex<Option<Vec<Artifact>>>>,
}

pub(crate) struct Artifact {
    id: u64,
    path: String,
    mime_type: String,
}

impl Transcript {
    pub(crate) fn new() -> Result<Self, String> {
        let working_directory = std::env::current_dir()
            .map_err(|error| format!("failed to find the current working directory: {error}"))?;
        Ok(Self(Arc::new(Mutex::new(TranscriptState {
            working_directory,
            active: None,
            failure: None,
        }))))
    }

    pub(crate) fn begin(
        &self,
        request_id: &RequestId,
        request_meta: &Meta,
        request: &CallToolRequestParams,
    ) -> Result<Call, String> {
        self.update(|state| {
            let active = state.materialize()?;
            active.next_call_id += 1;
            let call = Call {
                id: active.next_call_id,
                result_images: Arc::new(Mutex::new(None)),
            };
            let mut request = request.clone();
            if !request_meta.is_empty() {
                request.meta = Some(request_meta.clone());
            }
            active.append(
                json!({
                    "event": "tool_call",
                    "call_id": call.id,
                    "request_id": request_id,
                    "request": request,
                }),
                Utc::now(),
            )?;
            Ok(call)
        })
    }

    pub(crate) fn persist_image(
        &self,
        call_id: u64,
        data: &str,
        mime_type: &str,
    ) -> Result<Artifact, String> {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(data)
            .map_err(|error| format!("worker returned invalid base64 image data: {error}"));
        match bytes {
            Ok(bytes) => {
                self.update(|state| state.active()?.persist_image(call_id, &bytes, mime_type))
            }
            Err(error) => self.update(|_| Err(error)),
        }
    }

    pub(crate) fn finish(
        &self,
        call: Call,
        response: &Result<CallToolResult, ErrorData>,
    ) -> Result<(), String> {
        let result_images = call.take_result_images();
        match result_images {
            Ok(images) => self.update(|state| state.active()?.finish(call.id, images, response)),
            Err(error) => self.update(|_| Err(error)),
        }
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, TranscriptState>, String> {
        self.0
            .lock()
            .map_err(|_| "transcript lock poisoned".to_string())
    }

    fn update<T>(
        &self,
        operation: impl FnOnce(&mut TranscriptState) -> Result<T, String>,
    ) -> Result<T, String> {
        let mut state = self.lock()?;
        state.ensure_available()?;
        let result = operation(&mut state);
        if let Err(error) = &result {
            state.failure = Some(error.clone());
        }
        result
    }
}

impl TranscriptState {
    fn materialize(&mut self) -> Result<&mut ActiveTranscript, String> {
        if self.active.is_none() {
            self.active = Some(ActiveTranscript::create(&self.working_directory)?);
        }
        self.active()
    }

    fn active(&mut self) -> Result<&mut ActiveTranscript, String> {
        self.active
            .as_mut()
            .ok_or_else(|| "transcript was not materialized before recording output".to_string())
    }

    fn ensure_available(&self) -> Result<(), String> {
        match &self.failure {
            Some(error) => Err(format!(
                "transcript is unavailable after a recording failure: {error}"
            )),
            None => Ok(()),
        }
    }
}

impl ActiveTranscript {
    fn create(working_directory: &Path) -> Result<Self, String> {
        let working_directory_text = working_directory.to_string_lossy();
        let started_at = Utc::now();
        let run_id = format!(
            "{}-{}",
            started_at.format("%Y%m%dT%H%M%S%.9fZ"),
            std::process::id()
        );
        let sessions = working_directory.join(".mcp-console/sessions");
        let directory = sessions.join(&run_id);
        create_private_directory(&sessions, true)
            .map_err(|error| format!("failed to create {}: {error}", sessions.display()))?;
        create_private_directory(&directory, false)
            .map_err(|error| format!("failed to create {}: {error}", directory.display()))?;
        create_private_directory(&directory.join("artifacts"), false).map_err(|error| {
            format!(
                "failed to create {}: {error}",
                directory.join("artifacts").display()
            )
        })?;
        let internal = directory.join("internal");
        create_private_directory(&internal, false)
            .map_err(|error| format!("failed to create {}: {error}", internal.display()))?;
        let journal = internal.join("events.jsonl");
        let writer = create_private_file(&journal)
            .map(BufWriter::new)
            .map_err(|error| format!("failed to create {}: {error}", journal.display()))?;

        let mut transcript = Self {
            directory,
            writer,
            run_id,
            sequence: 0,
            next_call_id: 0,
            next_artifact_id: 0,
        };
        transcript.append(
            json!({
                "event": "session_started",
                "session": "default",
                "working_directory": working_directory_text,
            }),
            started_at,
        )?;
        Ok(transcript)
    }
}

impl Call {
    pub(crate) fn id(&self) -> u64 {
        self.id
    }

    pub(crate) fn record_result_images(&self, images: Vec<Artifact>) -> Result<(), String> {
        let mut result_images = self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?;
        if result_images.is_some() {
            return Err(format!(
                "tool call {} already retained result images",
                self.id
            ));
        }
        *result_images = Some(images);
        Ok(())
    }

    fn take_result_images(&self) -> Result<Vec<Artifact>, String> {
        Ok(self
            .result_images
            .lock()
            .map_err(|_| "tool call artifact lock poisoned".to_string())?
            .take()
            .unwrap_or_default())
    }
}

impl ActiveTranscript {
    fn append(&mut self, mut event: Value, at: DateTime<Utc>) -> Result<(), String> {
        let sequence = self.sequence + 1;
        let fields = event
            .as_object_mut()
            .ok_or_else(|| "transcript event must be a JSON object".to_string())?;
        fields.insert("schema_version".to_string(), json!(SCHEMA_VERSION));
        fields.insert("run_id".to_string(), json!(self.run_id));
        fields.insert("sequence".to_string(), json!(sequence));
        fields.insert("at".to_string(), json!(timestamp(at)));

        let mut record = serde_json::to_vec(&event)
            .map_err(|error| format!("failed to serialize transcript event: {error}"))?;
        record.push(b'\n');
        self.writer
            .write_all(&record)
            .and_then(|()| self.writer.flush())
            .map_err(|error| format!("failed to append transcript event: {error}"))?;
        self.sequence = sequence;
        Ok(())
    }

    fn finish(
        &mut self,
        call_id: u64,
        result_images: Vec<Artifact>,
        response: &Result<CallToolResult, ErrorData>,
    ) -> Result<(), String> {
        let event = match response {
            Ok(result) => json!({
                "event": "tool_result",
                "call_id": call_id,
                "result": self.project_result(call_id, result_images, result)?,
            }),
            Err(error) => {
                if !result_images.is_empty() {
                    return Err("tool error unexpectedly retained result images".to_string());
                }
                json!({
                    "event": "tool_result",
                    "call_id": call_id,
                    "error": error,
                })
            }
        };
        self.append(event, Utc::now())
    }

    fn persist_image(
        &mut self,
        call_id: u64,
        bytes: &[u8],
        mime_type: &str,
    ) -> Result<Artifact, String> {
        let artifact_id = self.next_artifact_id + 1;
        let extension = match mime_type {
            "image/png" => "png",
            _ => "bin",
        };
        let filename = format!("call-{call_id:06}-image-{artifact_id:06}.{extension}");
        let relative_path = format!("artifacts/{filename}");
        write_new(&self.directory.join(&relative_path), bytes)?;
        self.append(
            json!({
                "event": "artifact_created",
                "artifact_id": artifact_id,
                "call_id": call_id,
                "path": relative_path,
                "mime_type": mime_type,
                "bytes": bytes.len(),
            }),
            Utc::now(),
        )?;
        self.next_artifact_id = artifact_id;
        Ok(Artifact {
            id: artifact_id,
            path: relative_path,
            mime_type: mime_type.to_string(),
        })
    }

    fn project_result(
        &mut self,
        call_id: u64,
        result_images: Vec<Artifact>,
        result: &CallToolResult,
    ) -> Result<Value, String> {
        let mut value = serde_json::to_value(result)
            .map_err(|error| format!("failed to serialize tool result: {error}"))?;
        let recorded_content = value
            .get_mut("content")
            .and_then(Value::as_array_mut)
            .ok_or_else(|| "serialized tool result has no content array".to_string())?;
        let mut result_images = result_images.into_iter();

        for (index, content) in result.content.iter().enumerate() {
            let ContentBlock::Image(image) = content else {
                continue;
            };
            let artifact = result_images.next().ok_or_else(|| {
                format!("tool call {call_id} returned an image without a retained artifact")
            })?;
            if image.mime_type != artifact.mime_type {
                return Err(format!(
                    "tool call {call_id} returned an image with a different MIME type than its artifact"
                ));
            }

            let recorded_image = recorded_content[index]
                .as_object_mut()
                .ok_or_else(|| "serialized image content is not an object".to_string())?;
            recorded_image.remove("data");
            recorded_image.insert("artifactId".to_string(), json!(artifact.id));
            recorded_image.insert("path".to_string(), json!(artifact.path));
        }
        if result_images.next().is_some() {
            return Err(format!(
                "tool call {call_id} retained more artifacts than it returned"
            ));
        }
        Ok(value)
    }
}

fn timestamp(at: DateTime<Utc>) -> String {
    at.to_rfc3339_opts(SecondsFormat::Nanos, true)
}

fn create_private_directory(path: &Path, recursive: bool) -> std::io::Result<()> {
    let mut builder = DirBuilder::new();
    builder.recursive(recursive);
    #[cfg(unix)]
    builder.mode(0o700);
    builder.create(path)
}

fn create_private_file(path: &Path) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    options.mode(0o600);
    options.open(path)
}

fn write_new(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = create_private_file(path)
        .map_err(|error| format!("failed to create {}: {error}", path.display()))?;
    file.write_all(bytes)
        .and_then(|()| file.flush())
        .map_err(|error| format!("failed to write {}: {error}", path.display()))
}
