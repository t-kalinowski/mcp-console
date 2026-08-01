use serde::{Deserialize, Serialize};

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum ServerMessage {
    Evaluate { r: String },
    Input { stdin: String },
    Shutdown,
}

#[derive(Deserialize, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum WorkerMessage {
    Ready,
    Output { data: String },
    InputRequested { prompt: String },
    Completed,
    Fatal { message: String },
}
