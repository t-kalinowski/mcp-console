//! Load YAML 1.2 with Saphyr's event API and scalar resolver. Its ordinary mapping
//! loader overwrites duplicate keys, so check keys before constructing JSON maps.

use std::collections::BTreeMap;
use std::iter::Peekable;
use std::vec::IntoIter;

use saphyr::{Scalar, ScalarStyle};
use saphyr_parser::{Event, Parser, Span};
use serde_json::Value;

struct Loader<'a> {
    events: Peekable<IntoIter<(Event<'a>, Span)>>,
    anchors: BTreeMap<usize, Value>,
}

pub(super) fn load(source: &str) -> Result<Value, String> {
    let events = Parser::new_from_str(source)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| error.to_string())?;
    let mut loader = Loader {
        events: events.into_iter().peekable(),
        anchors: BTreeMap::new(),
    };
    loader.events.next(); // StreamStart
    if !matches!(loader.events.next(), Some((Event::DocumentStart(_), _))) {
        return Err("expected one mapping document".into());
    }
    let value = loader.node()?;
    if !value.is_object()
        || !matches!(loader.events.next(), Some((Event::DocumentEnd, _)))
        || !matches!(loader.events.next(), Some((Event::StreamEnd, _)))
    {
        return Err("expected one mapping document".into());
    }
    Ok(value)
}

impl Loader<'_> {
    fn node(&mut self) -> Result<Value, String> {
        let (event, span) = self.events.next().ok_or("expected a YAML value")?;
        let error = |message: &str| {
            format!(
                "line {}, column {}: {message}",
                span.start.line(),
                span.start.col() + 1
            )
        };
        let (anchor, value) = match event {
            Event::Scalar(text, style, anchor, tag) => {
                if tag.as_ref().is_some_and(|tag| {
                    !tag.is_yaml_core_schema()
                        || !matches!(
                            tag.suffix.as_ref(),
                            "null" | "bool" | "int" | "float" | "str"
                        )
                }) {
                    return Err(error("custom tags are unsupported"));
                }
                // Explicit core tags select the type even for quoted spellings.
                let style = if tag.is_some() {
                    ScalarStyle::Plain
                } else {
                    style
                };
                let scalar = Scalar::parse_from_cow_and_metadata(text, style, tag.as_ref())
                    .ok_or_else(|| error("invalid scalar or unsupported tag"))?;
                let value = match scalar {
                    Scalar::Null => Value::Null,
                    Scalar::Boolean(value) => value.into(),
                    Scalar::Integer(value) => value.into(),
                    Scalar::FloatingPoint(value) => serde_json::Number::from_f64(value.0)
                        .ok_or_else(|| error("non-finite numbers are unsupported"))?
                        .into(),
                    Scalar::String(value) => value.into_owned().into(),
                };
                (anchor, value)
            }
            Event::MappingStart(anchor, tag) => {
                if tag
                    .as_ref()
                    .is_some_and(|tag| !tag.is_yaml_core_schema() || tag.suffix != "map")
                {
                    return Err(error("unsupported mapping tag"));
                }
                let mut values = serde_json::Map::new();
                while !matches!(self.events.peek(), Some((Event::MappingEnd, _))) {
                    let key_span = self.events.peek().ok_or("expected a mapping key")?.1;
                    let Value::String(key) = self.node()? else {
                        return Err(error("mapping keys must be strings"));
                    };
                    if values.contains_key(&key) {
                        return Err(format!(
                            "line {}, column {}: duplicate key '{key}'",
                            key_span.start.line(),
                            key_span.start.col() + 1
                        ));
                    }
                    values.insert(key, self.node()?);
                }
                self.events.next();
                (anchor, Value::Object(values))
            }
            Event::SequenceStart(anchor, tag) => {
                if tag
                    .as_ref()
                    .is_some_and(|tag| !tag.is_yaml_core_schema() || tag.suffix != "seq")
                {
                    return Err(error("unsupported sequence tag"));
                }
                let mut values = Vec::new();
                while !matches!(self.events.peek(), Some((Event::SequenceEnd, _))) {
                    values.push(self.node()?);
                }
                self.events.next();
                (anchor, Value::Array(values))
            }
            Event::Alias(anchor) => {
                return self
                    .anchors
                    .get(&anchor)
                    .cloned()
                    .ok_or_else(|| error("unknown or recursive alias"));
            }
            _ => return Err(error("expected a YAML value")),
        };
        if anchor != 0 {
            self.anchors.insert(anchor, value.clone());
        }
        Ok(value)
    }
}
