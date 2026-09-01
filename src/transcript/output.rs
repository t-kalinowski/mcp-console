use std::fs::File;
use std::io::{BufWriter, Write};

use super::{Transcript, create_private_file};

const MAX_CELL_OUTPUT_BYTES: u64 = 1024 * 1024 * 1024;
const OUTPUT_BUFFER_BYTES: usize = 64 * 1024;

/// One private append-only file containing explicit text produced by a cell.
///
/// The pending-output tape remains independently bounded for MCP projection.
/// This file retains the same console text and direct stdout/stderr bytes before
/// that projection discards overflow. Images remain separate transcript
/// artifacts.
pub(crate) struct CellOutput {
    writer: Option<BufWriter<File>>,
    path: String,
    retained_bytes: u64,
    discarded_bytes: u64,
    retention_limit_reported: bool,
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
        let file_path = active.directory.join("outputs").join(&filename);
        let file = create_private_file(&file_path)
            .map_err(|error| format!("failed to create {}: {error}", file_path.display()))?;
        let path = format!(
            ".mcp-console/sessions/{}/outputs/{filename}",
            active.run_id
        );
        Ok(Some(CellOutput {
            writer: Some(BufWriter::with_capacity(OUTPUT_BUFFER_BYTES, file)),
            path,
            retained_bytes: 0,
            discarded_bytes: 0,
            retention_limit_reported: false,
        }))
    }
}

impl CellOutput {
    pub(crate) fn path(&self) -> &str {
        &self.path
    }

    /// Appends bytes while the retention limit permits it.
    ///
    /// A returned message is a server-owned notice that should be published
    /// after the corresponding ordinary output event. Further writes are still
    /// drained from the worker even after persistence stops.
    pub(crate) fn append(&mut self, bytes: &[u8]) -> Option<String> {
        if bytes.is_empty() {
            return None;
        }
        let remaining = MAX_CELL_OUTPUT_BYTES.saturating_sub(self.retained_bytes);
        let retained = bytes.len().min(remaining as usize);
        if retained > 0 {
            let Some(writer) = self.writer.as_mut() else {
                self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
                return None;
            };
            if let Err(error) = writer.write_all(&bytes[..retained]) {
                self.writer = None;
                self.discarded_bytes = self.discarded_bytes.saturating_add(bytes.len() as u64);
                return Some(format!(
                    "cell output file {} stopped after {} retained bytes: {error}",
                    self.path, self.retained_bytes
                ));
            }
            self.retained_bytes = self.retained_bytes.saturating_add(retained as u64);
        }
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
            "cell output retention limit reached at {MAX_CELL_OUTPUT_BYTES} bytes for {}; later output is being discarded",
            self.path
        ))
    }

    pub(crate) fn flush(&mut self) -> Option<String> {
        let writer = self.writer.as_mut()?;
        if let Err(error) = writer.flush() {
            self.writer = None;
            return Some(format!(
                "cell output file {} stopped after {} retained bytes: {error}",
                self.path, self.retained_bytes
            ));
        }
        None
    }

    pub(crate) fn finish(&mut self) -> Option<String> {
        let notice = self.flush();
        self.writer = None;
        notice
    }
}
