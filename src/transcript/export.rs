//! Reconstruct one session at its current cell's execution boundary. The server
//! supplies content only; the sandboxed worker owns every destination write.
use super::{Interval, Owner, ResultPart, Transcript};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, VecDeque};
use std::fmt::Write as _;
use std::io::{BufRead as _, BufReader, Write as _};
use std::path::{Path, PathBuf};

#[derive(Serialize, Deserialize)]
pub(crate) struct SessionExport {
    recording_directory: String,
    qmd: String,
    markdown: String,
    images: Vec<ExportImage>,
}
#[derive(Serialize, Deserialize)]
struct ExportImage {
    name: String,
    #[serde(skip)]
    source: PathBuf,
}

impl Transcript {
    pub(crate) fn export_session(&self) -> Result<SessionExport, String> {
        let (state, poisoned) = self.lock();
        if poisoned {
            return Err("session capture lock poisoned".into());
        }
        if let Some(error) = &state.failure {
            return Err(format!("session capture unavailable: {error}"));
        }
        let active = state.active.as_ref().ok_or("session capture unavailable")?;
        let owner = active
            .owner
            .filter(|o| o.interval == Interval::Cell)
            .ok_or("session export requires an executing cell")?;
        let call = active
            .cell_call
            .ok_or("session export has no calling cell")?;
        if active.temporary.is_some() {
            return Err("session recording has not been activated".into());
        }
        let journal = std::fs::File::open(active.directory.join("capture/events.jsonl"))
            .map_err(|e| format!("session capture unavailable: {e}"))?;
        let mut events = Vec::new();
        for line in BufReader::new(journal).lines() {
            let mut event: Value = serde_json::from_str(&line.map_err(|e| e.to_string())?)
                .map_err(|e| format!("invalid session capture: {e}"))?;
            if event["sequence"]
                .as_u64()
                .ok_or("capture sequence missing")?
                > active.snapshot_sequence
            {
                break;
            }
            if event["call_id"].as_u64() != Some(call)
                && matches!(
                    event["event"].as_str(),
                    Some(
                        "recording_started"
                            | "environment_discovered"
                            | "worker_started"
                            | "worker_ended"
                            | "python_environment_accepted"
                            | "requirements_selected"
                            | "requirements_accepted"
                            | "tool_call"
                            | "source_admitted"
                            | "source_finished"
                            | "source_associated"
                            | "cell_started"
                            | "tool_result"
                            | "delivery"
                            | "output_text"
                            | "artifact_created"
                    )
                )
            {
                // Reports need submitted source and dependency declarations,
                // not cumulative stdin or unrelated request metadata.
                if event["event"] == "tool_call" {
                    if let Some(request) = event["request"].as_object_mut() {
                        request.retain(|key, _| matches!(key.as_str(), "name" | "arguments"));
                    }
                    if let Some(arguments) = event["request"]["arguments"].as_object_mut() {
                        arguments.retain(|key, _| {
                            matches!(key.as_str(), "r" | "python" | "sql" | "requirements")
                        });
                    }
                }
                events.push(event);
            }
        }
        reconstruct(&active.directory, owner.session_id, &events)
    }
}

fn reconstruct(directory: &Path, session: u64, events: &[Value]) -> Result<SessionExport, String> {
    let mut documents = super::markdown::Documents::default();
    let mut calls = BTreeMap::new();
    let mut associations = BTreeMap::new();
    let mut cells = BTreeMap::new();
    let mut deliveries = BTreeMap::new();
    let mut results = BTreeMap::<String, String>::new();
    let mut order = Vec::<String>::new();
    let mut positions = BTreeMap::<String, u64>::new();
    let mut images = BTreeMap::<String, ExportImage>::new();
    for event in events {
        documents.observe(event)?;
        let sequence = event["sequence"]
            .as_u64()
            .ok_or("capture sequence missing")?;
        match event["event"].as_str().unwrap_or("") {
            "worker_started" if event["session_id"] == session => {
                positions.insert("Startup output".into(), sequence);
            }
            "tool_call" => {
                calls.insert(event["call_id"].as_u64().ok_or("call ID missing")?, event);
            }
            "source_associated" => {
                associations.insert(
                    event["call_id"].as_u64().ok_or("call ID missing")?,
                    event["session_id"].as_u64().ok_or("session missing")?,
                );
            }
            "cell_started" => {
                let owner: Owner =
                    serde_json::from_value(event["owner"].clone()).map_err(|e| e.to_string())?;
                if owner.session_id == session {
                    positions.insert(interval_key(owner), sequence);
                }
                cells.insert(event["call_id"].as_u64().ok_or("call ID missing")?, owner);
            }
            "delivery" => {
                if let Some(id) = event["call_id"].as_u64() {
                    deliveries.insert(id, event["delivered"].as_bool() == Some(true));
                }
            }
            _ => {}
        }
    }
    // Source may be associated before startup finishes. Actual lifecycle events
    // establish section order; later poll results join their recorded interval.
    for event in events {
        let sequence = event["sequence"]
            .as_u64()
            .ok_or("capture sequence missing")?;
        let call_id = event["call_id"].as_u64();
        if matches!(
            event["event"].as_str(),
            Some("output_text" | "artifact_created")
        ) {
            let owner: Owner =
                serde_json::from_value(event["owner"].clone()).map_err(|e| e.to_string())?;
            if owner.session_id == session && owner.interval != Interval::Cell {
                let key = interval_key(owner);
                positions.entry(key.clone()).or_insert(sequence);
                section(&mut results, &mut order, key);
            }
        }
        if event["event"] == "source_associated" && event["session_id"] == session {
            let call = calls
                .get(&call_id.ok_or("call ID missing")?)
                .ok_or("source request missing")?;
            let owner = cells.get(&call_id.unwrap()).copied();
            let key = owner
                .map(interval_key)
                .unwrap_or_else(|| format!("Submission {}", call_id.unwrap()));
            positions.entry(key.clone()).or_insert(sequence);
            let document = section(&mut results, &mut order, key);
            for language in ["r", "python", "sql"] {
                if let Some(source) = call["request"]["arguments"][language].as_str() {
                    super::markdown::push_fence(document, language, source);
                }
            }
        }
        if event["event"] != "tool_result" {
            continue;
        }
        let call_id = call_id.ok_or("result call ID missing")?;
        let call_session = associations.get(&call_id).copied();
        let parts: Vec<ResultPart> = serde_json::from_value(event["parts"].clone())
            .map_err(|e| format!("result associations unavailable: {e}"))?;
        let owns_response = call_session == Some(session)
            || parts
                .iter()
                .any(|p| p.owner.is_some_and(|o| o.session_id == session));
        if !owns_response {
            continue;
        }
        let delivered = deliveries.get(&call_id).copied().unwrap_or(false);
        let notice_key = format!("Response notices {call_id}");
        positions.insert(notice_key.clone(), sequence);
        let response_key = if calls.get(&call_id).is_some_and(|call| {
            ["r", "python", "sql"]
                .iter()
                .any(|language| call["request"]["arguments"][language].is_string())
        }) {
            cells
                .get(&call_id)
                .copied()
                .map(interval_key)
                .unwrap_or_else(|| format!("Submission {call_id}"))
        } else {
            notice_key.clone()
        };
        positions.entry(response_key.clone()).or_insert(sequence);
        if let Some(error) = event.get("error") {
            let document = section(&mut results, &mut order, response_key.clone());
            if !delivered {
                document.push_str("_Response assembled; transport write not confirmed._\n\n");
            }
            super::markdown::push_fence(
                document,
                "text",
                error["message"].as_str().unwrap_or("Tool error"),
            );
        }
        let Some(content) = event["result"]["content"].as_array() else {
            continue;
        };
        if parts.is_empty() {
            // Requirement-only and protocol error responses have no output tape.
            let document = section(&mut results, &mut order, response_key);
            if !delivered {
                document.push_str("_Response assembled; transport write not confirmed._\n\n");
            }
            for block in content {
                if let Some(text) = block["text"].as_str() {
                    super::markdown::push_fence(document, "text", text);
                }
            }
        }
        for part in &parts {
            if part.owner.is_some_and(|o| o.session_id != session) {
                continue;
            }
            let key = part
                .owner
                .map(interval_key)
                .unwrap_or_else(|| notice_key.clone());
            positions.entry(key.clone()).or_insert(sequence);
            let document = section(&mut results, &mut order, key);
            if !delivered {
                document.push_str("_Response assembled; transport write not confirmed._\n\n");
            }
            let block = content.get(part.block).ok_or("result block unavailable")?;
            if let Some(text) = block["text"].as_str() {
                let text = text
                    .get(part.start..part.end)
                    .ok_or("result text range unavailable")?;
                super::markdown::push_fence(document, "text", text);
            } else if block["type"] == "image" {
                let path = block["path"].as_str().ok_or("recorded image unavailable")?;
                // PNG and JPEG use format extensions; other MIME types remain binary.
                let extension = match block["mimeType"]
                    .as_str()
                    .ok_or("recorded image MIME type unavailable")?
                {
                    "image/png" => "png",
                    "image/jpeg" => "jpg",
                    _ => "bin",
                };
                let name = format!(
                    "image-{:06}.{extension}",
                    block["artifactId"]
                        .as_u64()
                        .ok_or("image identity missing")?
                );
                if !images.contains_key(path) {
                    let capture = directory
                        .join("capture/sessions")
                        .canonicalize()
                        .map_err(|e| format!("image capture unavailable: {e}"))?;
                    let image = directory
                        .join(path)
                        .canonicalize()
                        .map_err(|e| format!("recorded image {path} unavailable: {e}"))?;
                    if !image.starts_with(&capture) {
                        return Err("recorded image is outside this recording's capture".into());
                    }
                    images.insert(
                        path.into(),
                        ExportImage {
                            name: name.clone(),
                            source: image,
                        },
                    );
                }
                writeln!(document, "![Recorded image](<images/{name}>)\n").unwrap();
            }
        }
    }
    let qmd = documents
        .sessions
        .get(&session)
        .ok_or("session metadata unavailable")?
        .render("session.qmd");
    let mut markdown = format!(
        "# MCP Console session {session:03}\n\nResults below were assembled before the exporting cell began. A completed transport write is not confirmation that the client or model read it. Output previews and omission notices are preserved; supplementary raw logs are not expanded or copied.\n\n"
    );
    order.sort_by_key(|key| positions[key]);
    for key in order {
        if !results[&key].is_empty() {
            let heading = if key.starts_with("Response notices ") {
                "Response notices"
            } else {
                &key
            };
            writeln!(markdown, "## {heading}\n\n{}", results[&key]).unwrap();
        }
    }
    Ok(SessionExport {
        recording_directory: directory
            .canonicalize()
            .map_err(|e| format!("recording location unavailable: {e}"))?
            .into_os_string()
            .into_string()
            .map_err(|_| "session export requires a UTF-8 recording path")?,
        qmd,
        markdown,
        images: images.into_values().collect(),
    })
}
fn section<'a>(
    sections: &'a mut BTreeMap<String, String>,
    order: &mut Vec<String>,
    key: String,
) -> &'a mut String {
    if !sections.contains_key(&key) {
        order.push(key.clone());
    }
    sections.entry(key).or_default()
}
fn interval_key(owner: Owner) -> String {
    match owner.interval {
        Interval::Startup => "Startup output".into(),
        Interval::Cell => format!("Cell {:03}", owner.cell_id.expect("cell identity")),
        Interval::Post => format!("After cell {:03}", owner.cell_id.expect("post identity")),
    }
}

impl SessionExport {
    /// The server retains only this snapshot's ordered image paths. The worker
    /// requests the next payload after writing the previous one; it supplies
    /// neither a capture path nor a destination to the server.
    pub(crate) fn take_image_sources(&mut self) -> VecDeque<PathBuf> {
        self.images
            .iter_mut()
            .map(|image| std::mem::take(&mut image.source))
            .collect()
    }
    /// Executed inside the worker. No destination ever travels to the server.
    pub(crate) fn write(
        self,
        directory: &Path,
        mut next_image: impl FnMut() -> Result<String, String>,
    ) -> Result<Vec<PathBuf>, String> {
        let absolute = if directory.is_absolute() {
            directory.to_owned()
        } else {
            std::env::current_dir()
                .map_err(|e| e.to_string())?
                .join(directory)
        };
        let mut ancestor = absolute.as_path();
        let mut suffix = Vec::new();
        while !ancestor.exists() {
            // An unresolved `..` has no file_name and is rejected here, before
            // creating parents. Do not normalize it past unknown symlinks.
            suffix.push(ancestor.file_name().ok_or("invalid export destination")?);
            ancestor = ancestor.parent().ok_or("invalid export destination")?;
        }
        let mut resolved = ancestor.canonicalize().map_err(|e| e.to_string())?;
        for component in suffix.into_iter().rev() {
            resolved.push(component);
        }
        if resolved.starts_with(Path::new(&self.recording_directory)) {
            return Err("export destination must be outside the managed recording".into());
        }
        let absolute = resolved;
        let parent = absolute
            .parent()
            .ok_or("export destination needs a parent directory")?;
        std::fs::create_dir_all(parent).map_err(|e| format!("cannot create export parent: {e}"))?;
        std::fs::create_dir(&absolute).map_err(|e| {
            format!(
                "cannot create export destination {} (it must not already exist): {e}",
                directory.display()
            )
        })?;
        let mut paths = Vec::new();
        let result = (|| {
            for (name, bytes) in [
                ("session.qmd", self.qmd.as_bytes()),
                ("session.md", self.markdown.as_bytes()),
            ] {
                let path = absolute.join(name);
                let mut file = super::create_private_file(&path).map_err(|e| e.to_string())?;
                paths.push(path);
                file.write_all(bytes).map_err(|e| e.to_string())?;
            }
            if !self.images.is_empty() {
                std::fs::create_dir(absolute.join("images")).map_err(|e| e.to_string())?;
            }
            for image in self.images {
                let path = absolute.join("images").join(&image.name);
                let data = next_image()?;
                let bytes = base64::engine::general_purpose::STANDARD
                    .decode(&data)
                    .map_err(|e| e.to_string())?;
                let mut file = super::create_private_file(&path).map_err(|e| e.to_string())?;
                paths.push(path);
                file.write_all(&bytes).map_err(|e| e.to_string())?;
            }
            Ok(paths.clone())
        })();
        if result.is_err() {
            for path in &paths {
                let _ = std::fs::remove_file(path);
            }
            let _ = std::fs::remove_dir(absolute.join("images"));
            let _ = std::fs::remove_dir(&absolute);
        }
        result.map_err(|error: String| format!("session export failed: {error}"))
    }
}
