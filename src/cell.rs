use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub(crate) enum Language {
    R,
    Python,
}

pub(crate) struct Cell {
    pub(crate) language: Language,
    pub(crate) source: String,
}
