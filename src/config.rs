//! Schema-independent project-file and command-line configuration layering.

use serde_json::{Map, Value};

mod inline;
mod yaml;

/// Read one optional project mapping, then apply overrides in argument order.
/// Absence is distinct from an explicitly supplied empty configuration.
pub fn load(path: &str, overrides: &[String]) -> Result<Option<Value>, String> {
    // A dangling symlink or an unreadable existing file must reach read_to_string.
    let mut value = match std::fs::symlink_metadata(path) {
        Err(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::NotFound | std::io::ErrorKind::NotADirectory
            ) =>
        {
            None
        }
        Err(error) => return Err(format!("cannot inspect '{path}': {error}")),
        Ok(_) => {
            let source = std::fs::read_to_string(path)
                .map_err(|error| format!("cannot read '{path}': {error}"))?;
            Some(yaml::load(&source).map_err(|error| format!("{path}: {error}"))?)
        }
    };
    for argument in overrides {
        let (key, source) = argument
            .split_once('=')
            .ok_or_else(|| "invalid configuration override: expected KEY=VALUE".to_string())?;
        let key = key.trim();
        let context = |error| format!("invalid configuration override '{key}': {error}");
        let keys: Vec<_> = key.split('.').map(str::trim).collect();
        if keys.iter().any(|key| key.is_empty()) {
            return Err(context("expected a nonempty dotted key".into()));
        }
        let mut overlay = inline::parse(source).map_err(context)?;
        for key in keys.into_iter().rev() {
            overlay = Value::Object(Map::from_iter([(key.to_string(), overlay)]));
        }
        merge(
            value.get_or_insert_with(|| Value::Object(Map::new())),
            overlay,
        );
    }
    Ok(value)
}

/// Objects merge recursively; arrays, scalars, and null replace the old value.
/// Keys have no special meaning, including discriminator or profile fields.
fn merge(base: &mut Value, overlay: Value) {
    match (base, overlay) {
        (Value::Object(base), Value::Object(overlay)) => {
            for (key, value) in overlay {
                merge(base.entry(key).or_insert(Value::Null), value);
            }
        }
        (base, value) => *base = value,
    }
}
