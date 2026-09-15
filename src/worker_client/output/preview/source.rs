use super::Gap;

#[derive(Clone, Default)]
pub(in crate::worker_client::output) struct Source {
    pub(in crate::worker_client::output) file: Option<crate::transcript::OutputRecord>,
    pub(in crate::worker_client::output) raw_bytes: u64,
    pub(in crate::worker_client::output) retained_bytes: u64,
    pub(in crate::worker_client::output) discarded_bytes: u64,
}

impl Source {
    pub(super) fn notice(&self, omitted: u64, notices: u64) -> String {
        let location = match &self.file {
            Some(file) => format!(
                "raw cell log: {} (Console server recording workspace; controller for remote targets); {} raw bytes retained, {} raw bytes not retained{}",
                file.public_path(),
                self.retained_bytes,
                self.discarded_bytes,
                if self.discarded_bytes == 0 {
                    ""
                } else {
                    "; file contains only a prefix; omitted text beyond it is unavailable"
                },
            ),
            None => format!(
                "no retained cell log ({} raw bytes observed); omitted text is unavailable",
                self.raw_bytes
            ),
        };
        let generated = if notices == 0 {
            String::new()
        } else {
            format!("; {notices} generated notice bytes not retained")
        };
        format!(
            "\n[output preview: omitted {omitted} rendered UTF-8 bytes{generated}; {location}]\n"
        )
    }
}

/// Constant-size accounting for fully omitted response intervals. The journal
/// retains individual file paths and cumulative cell totals after receipts drop.
#[derive(Clone, Default)]
pub(in crate::worker_client::output) struct Summary {
    pub(super) gap: Gap,
    intervals: u64,
    retained_bytes: u64,
    discarded_bytes: u64,
    journal: Option<String>,
}

impl Summary {
    pub(super) fn new(source: &Source, gap: Gap) -> Self {
        let (retained_bytes, discarded_bytes, journal) = match &source.file {
            Some(file) => {
                // Active polls replay an unclaimed response before collecting
                // later output. Composition retires the old cell first, so its
                // journal entry can be published before releasing this receipt.
                file.note_inline_omission(gap.bytes);
                file.publish();
                // File retention only changes from accepting bytes to discarding
                // them. Limit its cumulative discarded count to this interval.
                let discarded = source.raw_bytes.min(source.discarded_bytes);
                let (session, _) = file
                    .public_path()
                    .rsplit_once("/outputs/")
                    .expect("cell output path");
                (
                    source.raw_bytes - discarded,
                    discarded,
                    Some(format!("{session}/internal/events.jsonl")),
                )
            }
            None => (0, source.raw_bytes, None),
        };
        Self {
            gap,
            intervals: u64::from(gap.bytes != 0),
            retained_bytes,
            discarded_bytes,
            journal,
        }
    }

    pub(super) fn extend(&mut self, other: Self) {
        self.gap.bytes += other.gap.bytes;
        self.gap.notices += other.gap.notices;
        self.intervals += other.intervals;
        self.retained_bytes += other.retained_bytes;
        self.discarded_bytes += other.discarded_bytes;
        if self.journal.is_none() {
            self.journal = other.journal;
        }
    }

    pub(super) fn notice(&self) -> String {
        let location = match &self.journal {
            Some(journal) => format!(
                "raw cell log paths and per-cell counts: {journal} (Console server recording workspace; controller for remote targets)"
            ),
            None => "no retained cell logs".to_owned(),
        };
        format!(
            "\n[output preview: omitted {} rendered UTF-8 bytes across {} output intervals; {} generated notice bytes not retained; {} raw bytes retained, {} raw bytes not retained; {location}; unretained text is unavailable]\n",
            self.gap.bytes,
            self.intervals,
            self.gap.notices,
            self.retained_bytes,
            self.discarded_bytes,
        )
    }
}
