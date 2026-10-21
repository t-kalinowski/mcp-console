//! Reconstruct one session at its current cell's execution boundary. The server
//! supplies content only; the sandboxed worker owns every destination write.
use super::{Interval, Owner, ResultPart, Transcript};
use base64::Engine as _;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, VecDeque};
use std::fs::File;
use std::io::{BufRead as _, BufReader, Read as _, Seek as _, SeekFrom, Write as _};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Serialize, Deserialize)]
pub(crate) struct SessionExport {
    recording_directory: String,
    files: Vec<String>,
}

/// Spools belong to this runtime callback and are removed on completion, error,
/// replacement, or shutdown. Only server-selected files can cross the sideband.
pub(crate) struct ExportContents {
    current: Option<std::io::Take<File>>,
    files: VecDeque<std::io::Take<File>>,
    // Drop open files before removing the spool directory on Windows.
    _temporary: tempfile::TempDir,
}
impl ExportContents {
    pub(crate) fn next_chunk(&mut self) -> Result<Option<String>, String> {
        if self.current.is_none() {
            self.current = Some(self.files.pop_front().ok_or("no pending export file")?);
        }
        let mut bytes = [0; 64 * 1024];
        let count = self
            .current
            .as_mut()
            .unwrap()
            .read(&mut bytes)
            .map_err(|e| e.to_string())?;
        if count == 0 {
            self.current = None;
            Ok(None)
        } else {
            Ok(Some(
                base64::engine::general_purpose::STANDARD.encode(&bytes[..count]),
            ))
        }
    }
}

impl Transcript {
    pub(crate) fn export_session(
        &self,
        cancelled: &AtomicBool,
    ) -> Result<(SessionExport, ExportContents), String> {
        // Cell entry is queued before this callback. Flush it through the
        // recorder's normal failure owner before selecting the snapshot.
        self.update(|_| Ok(()));
        let (directory, session, snapshot_sequence, call) = {
            let (state, poisoned) = self.lock();
            if cancelled.load(Ordering::Acquire) {
                return Err("session export cancelled".into());
            }
            if poisoned {
                return Err("session capture lock poisoned".into());
            }
            if let Some(error) = &state.failure {
                return Err(format!("session capture unavailable: {error}"));
            }
            let active = state.active.as_ref().ok_or("session capture unavailable")?;
            let (call, owner, snapshot_sequence) = active
                .cell_snapshot
                .ok_or("session export requires an executing cell")?;
            if !active.published {
                return Err("session recording has not been activated".into());
            }
            (
                active.directory.clone(),
                owner.session_id,
                snapshot_sequence,
                call,
            )
        };
        reconstruct(&directory, session, snapshot_sequence, call, cancelled)
    }
}

/// Both passes read the same completed prefix while later capture can append.
/// Drop each event after visiting it: output chunk counts must not determine
/// export memory, and historical stdin belongs only to the journal.
fn scan_capture(
    directory: &Path,
    sequence: u64,
    exporting_call: u64,
    cancelled: &AtomicBool,
    mut visit: impl FnMut(u64, Value) -> Result<(), String>,
) -> Result<(), String> {
    let journal = std::fs::File::open(directory.join("capture/events.jsonl"))
        .map_err(|e| format!("session capture unavailable: {e}"))?;
    let mut journal = BufReader::new(journal);
    let mut offset = 0;
    loop {
        if cancelled.load(Ordering::Acquire) {
            return Err("session export cancelled".into());
        }
        let mut line = String::new();
        let count = journal.read_line(&mut line).map_err(|e| e.to_string())?;
        if count == 0 {
            break;
        }
        let event: Value =
            serde_json::from_str(&line).map_err(|e| format!("invalid session capture: {e}"))?;
        if event["sequence"]
            .as_u64()
            .ok_or("capture sequence missing")?
            > sequence
        {
            break;
        }
        if event["call_id"].as_u64() != Some(exporting_call) {
            visit(offset, event)?;
        }
        offset += count as u64;
    }
    Ok(())
}

fn reconstruct(
    directory: &Path,
    session: u64,
    snapshot_sequence: u64,
    exporting_call: u64,
    cancelled: &AtomicBool,
) -> Result<(SessionExport, ExportContents), String> {
    let temporary = tempfile::tempdir().map_err(|e| e.to_string())?;
    let mut source =
        super::create_private_file(&temporary.path().join("source")).map_err(|e| e.to_string())?;
    let mut documents = super::markdown::Documents::default();
    let mut calls = BTreeMap::new();
    let mut associations = BTreeMap::new();
    let mut cells = BTreeMap::new();
    let mut deliveries = BTreeMap::new();
    let mut sections = Sections::new(temporary.path());
    let mut positions = BTreeMap::<String, u64>::new();
    let mut images = BTreeMap::<String, (String, std::io::Take<File>)>::new();
    scan_capture(
        directory,
        snapshot_sequence,
        exporting_call,
        cancelled,
        |offset, event| {
            for id in documents.observe(&event)? {
                let fragment = documents.sessions.get_mut(&id).unwrap().take_source();
                if id == session {
                    source
                        .write_all(fragment.as_bytes())
                        .map_err(|e| e.to_string())?;
                }
            }
            let sequence = event["sequence"]
                .as_u64()
                .ok_or("capture sequence missing")?;
            match event["event"].as_str().unwrap_or("") {
                "worker_started" if event["session_id"] == session => {
                    positions.insert("Startup output".into(), sequence);
                }
                "tool_call" => {
                    if ["r", "python", "sql"]
                        .iter()
                        .any(|language| event["request"]["arguments"][language].is_string())
                    {
                        calls.insert(event["call_id"].as_u64().ok_or("call ID missing")?, offset);
                    }
                }
                "source_associated" => {
                    associations.insert(
                        event["call_id"].as_u64().ok_or("call ID missing")?,
                        event["session_id"].as_u64().ok_or("session missing")?,
                    );
                }
                "cell_started" => {
                    let owner: Owner = serde_json::from_value(event["owner"].clone())
                        .map_err(|e| e.to_string())?;
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
            Ok(())
        },
    )?;
    // Source may be associated before startup finishes. Actual lifecycle events
    // establish section order; later poll results join their recorded interval.
    let mut journal = BufReader::new(
        File::open(directory.join("capture/events.jsonl")).map_err(|e| e.to_string())?,
    );
    scan_capture(
        directory,
        snapshot_sequence,
        exporting_call,
        cancelled,
        |_, event| {
            let sequence = event["sequence"]
                .as_u64()
                .ok_or("capture sequence missing")?;
            let call_id = event["call_id"].as_u64();
            if matches!(
                event["event"].as_str(),
                Some("output_text" | "image_retained")
            ) {
                let owner: Owner =
                    serde_json::from_value(event["owner"].clone()).map_err(|e| e.to_string())?;
                if owner.session_id == session && owner.interval != Interval::Cell {
                    let key = interval_key(owner);
                    positions.entry(key.clone()).or_insert(sequence);
                }
            }
            if event["event"] == "source_associated" && event["session_id"] == session {
                let Some(offset) = calls.get(&call_id.ok_or("call ID missing")?) else {
                    return Ok(());
                };
                journal
                    .seek(SeekFrom::Start(*offset))
                    .map_err(|e| e.to_string())?;
                let mut line = String::new();
                journal.read_line(&mut line).map_err(|e| e.to_string())?;
                let call: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
                let owner = cells.get(&call_id.unwrap()).copied();
                let key = owner
                    .map(interval_key)
                    .unwrap_or_else(|| format!("Submission {}", call_id.unwrap()));
                positions.entry(key.clone()).or_insert(sequence);
                let mut document = sections.section(key)?;
                for language in ["r", "python", "sql"] {
                    if let Some(source) = call["request"]["arguments"][language].as_str() {
                        write_fence(&mut document, language, source)?;
                    }
                }
            }
            if event["event"] != "tool_result" {
                return Ok(());
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
                return Ok(());
            }
            let delivered = deliveries.get(&call_id).copied().unwrap_or(false);
            let notice_key = format!("Response notices {call_id}");
            positions.insert(notice_key.clone(), sequence);
            let response_key = if calls.contains_key(&call_id) {
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
                let mut document = sections.section(response_key.clone())?;
                if !delivered {
                    document
                        .write_all(b"_Response assembled; transport write not confirmed._\n\n")
                        .map_err(|e| e.to_string())?;
                }
                write_fence(
                    &mut document,
                    "text",
                    error["message"].as_str().unwrap_or("Tool error"),
                )?;
            }
            let Some(content) = event["result"]["content"].as_array() else {
                return Ok(());
            };
            if parts.is_empty() {
                // Requirement-only and protocol error responses have no output tape.
                let mut document = sections.section(response_key)?;
                if !delivered {
                    document
                        .write_all(b"_Response assembled; transport write not confirmed._\n\n")
                        .map_err(|e| e.to_string())?;
                }
                for block in content {
                    if let Some(text) = block["text"].as_str() {
                        write_fence(&mut document, "text", text)?;
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
                let mut document = sections.section(key)?;
                if !delivered {
                    document
                        .write_all(b"_Response assembled; transport write not confirmed._\n\n")
                        .map_err(|e| e.to_string())?;
                }
                let block = content.get(part.block).ok_or("result block unavailable")?;
                if let Some(text) = block["text"].as_str() {
                    let text = text
                        .get(part.start..part.end)
                        .ok_or("result text range unavailable")?;
                    write_fence(&mut document, "text", text)?;
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
                        let image = super::files::open_capture_image(directory, path)?;
                        let length = image.metadata().map_err(|e| e.to_string())?.len();
                        images.insert(path.into(), (name.clone(), image.take(length)));
                    }
                    writeln!(document, "![Recorded image](<images/{name}>)\n")
                        .map_err(|e| e.to_string())?;
                }
            }
            Ok(())
        },
    )?;
    let header = documents
        .sessions
        .get(&session)
        .ok_or("session metadata unavailable")?
        .render("session.qmd");
    let qmd_path = temporary.path().join("session.qmd");
    let markdown_path = temporary.path().join("session.md");
    let mut qmd = super::create_private_file(&qmd_path).map_err(|e| e.to_string())?;
    qmd.write_all(header.as_bytes())
        .map_err(|e| e.to_string())?;
    let mut source = File::open(temporary.path().join("source")).map_err(|e| e.to_string())?;
    std::io::copy(&mut source, &mut qmd).map_err(|e| e.to_string())?;
    let mut markdown = super::create_private_file(&markdown_path).map_err(|e| e.to_string())?;
    writeln!(markdown, "# MCP Console session {session:03}\n\nResults below were assembled before the exporting cell began. A completed transport write is not confirmation that the client or model read it. Output previews and omission notices are preserved; supplementary raw logs are not expanded or copied.\n").map_err(|e| e.to_string())?;
    sections.order.sort_by_key(|key| positions[key]);
    for key in sections.order {
        let path = &sections.paths[&key];
        if path.metadata().map_err(|e| e.to_string())?.len() == 0 {
            continue;
        }
        let heading = if key.starts_with("Response notices ") {
            "Response notices"
        } else {
            &key
        };
        writeln!(markdown, "## {heading}\n").map_err(|e| e.to_string())?;
        let mut section = File::open(path).map_err(|e| e.to_string())?;
        std::io::copy(&mut section, &mut markdown).map_err(|e| e.to_string())?;
        writeln!(markdown).map_err(|e| e.to_string())?;
    }
    let mut files = vec!["session.qmd".into(), "session.md".into()];
    let mut contents = VecDeque::new();
    for path in [qmd_path, markdown_path] {
        let file = File::open(path).map_err(|e| format!("export file unavailable: {e}"))?;
        let length = file.metadata().map_err(|e| e.to_string())?.len();
        contents.push_back(file.take(length));
    }
    for (name, image) in images.into_values() {
        files.push(format!("images/{name}"));
        contents.push_back(image);
    }
    Ok((
        SessionExport {
            recording_directory: directory
                .canonicalize()
                .map_err(|e| format!("recording location unavailable: {e}"))?
                .into_os_string()
                .into_string()
                .map_err(|_| "session export requires a UTF-8 recording path")?,
            files,
        },
        ExportContents {
            _temporary: temporary,
            files: contents,
            current: None,
        },
    ))
}

// Polls can append to earlier intervals. Spool each section separately so their
// final ordering needs only paths and positions, never accumulated result text.
struct Sections {
    directory: PathBuf,
    paths: BTreeMap<String, PathBuf>,
    order: Vec<String>,
}
impl Sections {
    fn new(directory: &Path) -> Self {
        Self {
            directory: directory.into(),
            paths: BTreeMap::new(),
            order: Vec::new(),
        }
    }
    fn section(&mut self, key: String) -> Result<File, String> {
        if let Some(path) = self.paths.get(&key) {
            return std::fs::OpenOptions::new()
                .append(true)
                .open(path)
                .map_err(|e| e.to_string());
        }
        let path = self.directory.join(format!("section-{}", self.order.len()));
        let file = super::create_private_file(&path).map_err(|e| e.to_string())?;
        self.paths.insert(key.clone(), path);
        self.order.push(key);
        Ok(file)
    }
}
fn write_fence(document: &mut File, language: &str, contents: &str) -> Result<(), String> {
    let mut fragment = String::new();
    super::markdown::push_fence(&mut fragment, language, contents);
    document
        .write_all(fragment.as_bytes())
        .map_err(|e| e.to_string())
}

fn interval_key(owner: Owner) -> String {
    match owner.interval {
        Interval::Startup => "Startup output".into(),
        Interval::Cell => format!("Cell {:03}", owner.cell_id.expect("cell identity")),
        Interval::Post => format!("After cell {:03}", owner.cell_id.expect("post identity")),
    }
}

impl SessionExport {
    /// Executed inside the worker. No destination ever travels to the server.
    pub(crate) fn write(
        self,
        directory: &Path,
        mut next_chunk: impl FnMut() -> Result<Option<String>, String>,
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
        if resolved.to_str().is_none() {
            return Err("export destination requires a UTF-8 path".into());
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
            for name in self.files {
                let path = absolute.join(&name);
                if name.starts_with("images/") {
                    std::fs::create_dir_all(absolute.join("images")).map_err(|e| e.to_string())?;
                }
                let mut file = super::create_private_file(&path).map_err(|e| e.to_string())?;
                paths.push(path);
                while let Some(data) = next_chunk()? {
                    let bytes = base64::engine::general_purpose::STANDARD
                        .decode(&data)
                        .map_err(|e| e.to_string())?;
                    file.write_all(&bytes).map_err(|e| e.to_string())?;
                }
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
