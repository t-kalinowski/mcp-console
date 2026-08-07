use serde::{Deserialize, Serialize};

use crate::cell::Language;

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate { language: Language, source: String },
    Shutdown,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum WorkerMessage {
    Ready,
    Output { data: String },
    Image { data: String, mime_type: String },
    InputRequested { prompt: String },
    InputReceived,
    Completed,
}
