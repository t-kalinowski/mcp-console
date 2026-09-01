use std::fs::File;
use std::io::Write;

use chrono::Utc;
use serde_json::json;

use super::{Transcript, create_private_file};

pub(super) const MAX_CELL_OUTPUT_BYTES: u64 = 1024 * 1024 * 1024;

/// One private append-only file containing explicit text produced by a cell.
///
/// The pending-output tape remains independently bounded for MCP projection.
/// This file retains console text and direct stdout/stderr bytes before that
/// projection discards overflow. Images remain separate transcript artifacts.
pub(crate) struct CellOutput {
    writer: Option<File>,
    transcript: Transcript,
    call_id: u64,
    relative_path: String,
    public_path: String,
    retained_bytes: u64,
    inline_omitted_bytes: u64,
    discarded_bytes: u64,
    retention_limit_reported: bool,
}

struct CellOutputSummary {
    retained_bytes: u64,
    inline_omitted_bytes: u64,
    discarded_bytes: u64,
}

impl Transcript {
    pub(crate) fn create_cell_output(
        &self,
        call_id: Option<u64>,
    ) -> Result<Option<CellOutput>, String> {
        let Some(call_id) = call_id else {
            return Ok(None);
        };
        let (mut state, poisoned) = self.lock();
        if poisoned {
            return Err("transcript lock poisoned while creating cell output".to_string());
        }
        if state.failure.is_some() {
            return Ok(None);
        }
        let active = state.active()?;
        let filename = format!("call-{call_id:06}.log");
        let relative_path = format!("outputs/{filename}");
        let file_path = active.directory.join(&relative_path);
        let public_path = format!(".mcp-console/sessions/{}/outputs/{filename}", active.run_id);
        let writer = create_private_file(&file_path)
            .map_err(|error| format!("failed to create {public_path}: {error}"))?;
        Ok(Some(CellOutput {
            writer: Some(writer),
            transcript: self.clone(),
            call_id,
            relative_path,
            public_path,
            retained_bytes: 0,
            inline_omitted_bytes: 0,
            discarded_bytes: 0,
            retention_limit_reported: false,
        }))
    }

    fn record_cell_output(&self, call_id: u64, path: &str, summary: CellOutputSummary) {
        self.update(|state| {
            state.active()?.append(
                json!({
                    "event": "cell_output",
                    "call_id": call_id,
                    "path": path,
                    "retained_bytes": summary.retained_bytes,
                    "inline_omitted_bytes": summary.inline_omitted_bytes,
                    "discarded_bytes": summary.discarded_bytes,
                    "retention_limit_bytes": MAX_CELL_OUTPUT_BYTES,
                }),
                Utc::now(),
            )
        });
    }
}

impl CellOutput {
    pub(crate) fn public_path(&self) -> &str {
        &self.public_path
    }

    /// Appends bytes while the retention limit permits it.
    ///
    /// A returned message is a server-owned notice that must be published after
    /// the corresponding ordinary output event. Further writes are still
    /// drained from the worker even after persistence stops.
    pub(crate) fn append(&mut self, bytes: &[u8]) -> Option<String> {
        if bytes.is_empty() {
            return None;
        }
        if self.writer.is_none() {
            self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
            return None;
        }

        let remaining = MAX_CELL_OUTPUT_BYTES.saturating_sub(self.retained_bytes);
        let retained = bytes
            .len()
            .min(usize::try_from(remaining).unwrap_or(usize::MAX));
        let write_result = (retained > 0).then(|| {
            self.writer
                .as_mut()
                .expect("cell output writer presence was checked")
                .write_all(&bytes[..retained])
        });
        if let Some(Err(error)) = write_result {
            let observed = self
                .writer
                .as_ref()
                .expect("failed cell output writer must remain available for inspection")
                .metadata()
                .map(|metadata| metadata.len())
                .unwrap_or(self.retained_bytes);
            let seen = self
                .retained_bytes
                .saturating_add(self.discarded_bytes)
                .saturating_add(bytes.len() as u64);
            self.retained_bytes = observed;
            self.discarded_bytes = seen.saturating_sub(observed);
            self.writer = None;
            return Some(format!(
                "cell output file {} stopped after {} retained bytes: {error}; later text is permanently discarded",
                self.public_path, self.retained_bytes
            ));
        }
        self.retained_bytes = self.retained_bytes.saturating_add(retained as u64);

        let discarded = bytes.len() - retained;
        if discarded == 0 {
            return None;
        }
        self.discarded_bytes = self.discarded_bytes.saturating_add(discarded as u64);
        if self.retention_limit_reported {
            return None;
        }
        self.retention_limit_reported = true;
        Some(format!(
            "cell output retention limit reached at {MAX_CELL_OUTPUT_BYTES} bytes for {}; later text is permanently discarded",
            self.public_path
        ))
    }

    pub(crate) fn note_inline_omission(&mut self, bytes: usize) {
        self.inline_omitted_bytes = self.inline_omitted_bytes.saturating_add(bytes as u64);
    }

    pub(crate) fn flush(&mut self) -> Option<String> {
        let writer = self.writer.as_mut()?;
        if let Err(error) = writer.flush() {
            self.writer = None;
            return Some(format!(
                "cell output file {} stopped after {} retained bytes: {error}; later text is permanently discarded",
                self.public_path, self.retained_bytes
            ));
        }
        None
    }

    pub(crate) fn finish(mut self) -> Option<String> {
        let notice = self.flush();
        self.writer = None;
        let summary = CellOutputSummary {
            retained_bytes: self.retained_bytes,
            inline_omitted_bytes: self.inline_omitted_bytes,
            discarded_bytes: self.discarded_bytes,
        };
        self.transcript
            .record_cell_output(self.call_id, &self.relative_path, summary);
        notice
    }
}
