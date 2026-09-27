//! Native launch values crossing the private JSON boundary retain Unix bytes.

pub(crate) mod path {
    use serde::{Deserialize, Serialize};
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};

    pub(crate) fn serialize<S: serde::Serializer>(
        path: &Path,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        path.as_os_str().serialize(serializer)
    }

    pub(crate) fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<PathBuf, D::Error> {
        OsString::deserialize(deserializer).map(PathBuf::from)
    }
}

pub(super) mod environment {
    use serde::{Deserialize, Serialize};
    use std::collections::BTreeMap;
    use std::ffi::OsString;

    pub(crate) fn serialize<S: serde::Serializer>(
        values: &BTreeMap<OsString, OsString>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        values.iter().collect::<Vec<_>>().serialize(serializer)
    }

    pub(crate) fn deserialize<'de, D: serde::Deserializer<'de>>(
        deserializer: D,
    ) -> Result<BTreeMap<OsString, OsString>, D::Error> {
        Vec::<(OsString, OsString)>::deserialize(deserializer)
            .map(|values| values.into_iter().collect())
    }
}
