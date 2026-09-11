//! Load YAML nodes with Saphyr, then convert the JSON-compatible subset.
//! Scalar and tag resolution belong to the loader.

use saphyr::{LoadableYamlNode, MarkedYaml, Scalar, YamlData};
use serde_json::Value;

pub(super) fn load(source: &str) -> Result<Value, String> {
    // Saphyr's node loader currently keeps the last value for duplicate keys.
    let documents = MarkedYaml::load_from_str(source).map_err(|error| error.to_string())?;
    let [document] = documents.as_slice() else {
        return Err("expected one mapping document".into());
    };
    if !matches!(document.data, YamlData::Mapping(_)) {
        return Err("expected one mapping document".into());
    }
    to_json(document)
}

fn to_json(node: &MarkedYaml<'_>) -> Result<Value, String> {
    let error = |message: &str| {
        format!(
            "line {}, column {}: {message}",
            node.span.start.line(),
            node.span.start.col() + 1
        )
    };
    match &node.data {
        YamlData::Value(value) => Ok(match value {
            Scalar::Null => Value::Null,
            Scalar::Boolean(value) => (*value).into(),
            Scalar::Integer(value) => (*value).into(),
            Scalar::FloatingPoint(value) => serde_json::Number::from_f64(value.0)
                .ok_or_else(|| error("non-finite numbers are unsupported"))?
                .into(),
            Scalar::String(value) => value.as_ref().into(),
        }),
        YamlData::Sequence(values) => values.iter().map(to_json).collect(),
        YamlData::Mapping(values) => values
            .iter()
            .map(|(key, value)| {
                let Value::String(key) = to_json(key)? else {
                    return Err(error("mapping keys must be strings"));
                };
                Ok((key, to_json(value)?))
            })
            .collect::<Result<serde_json::Map<_, _>, _>>()
            .map(Value::Object),
        YamlData::Tagged(..) => Err(error("custom tags are unsupported")),
        _ => Err(error("invalid or unresolved YAML value")),
    }
}
