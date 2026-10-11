//! Configuration-only matrices do not need a server process or a YAML transcript.
//! Real discovery, captured configuration, provider use, and startup failures
//! remain covered at the client/server boundary.

use super::*;

fn render(languages: Languages, builtin: bool) -> (Value, String) {
    let (router, instructions) = ConsoleServer::configured_presentation(
        languages,
        builtin,
        &SandboxSettings::new(),
        true,
        &crate::settings::Tool::default(),
    )
    .unwrap();
    let tool = &router.map.get("send").unwrap().attr;
    (serde_json::to_value(tool).unwrap(), instructions)
}

fn without_descriptions(value: &Value) -> Value {
    match value {
        Value::Object(object) => Value::Object(
            object
                .iter()
                .filter(|(key, _)| key.as_str() != "description")
                .map(|(key, value)| (key.clone(), without_descriptions(value)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn check_description_words(value: &Value, fields: &[&str]) {
    if let Value::Object(object) = value {
        for (key, value) in object {
            if key == "description" {
                for word in value
                    .as_str()
                    .unwrap()
                    .split(|character: char| !character.is_ascii_alphabetic())
                {
                    let word = word.to_ascii_lowercase();
                    if ["r", "python", "sql"].contains(&word.as_str()) {
                        assert!(fields.contains(&word.as_str()), "{value}");
                    }
                }
            } else {
                check_description_words(value, fields);
            }
        }
    }
}

fn check_guidance(tool: &Value, languages: Languages, builtin: bool) {
    let fields = languages.fields();
    check_description_words(tool, &fields);
    let description = tool["description"].as_str().unwrap();
    let properties = &tool["inputSchema"]["properties"];
    for instruction in [
        "ir run script.R",
        "#| packages:",
        "#| r-version:",
        "imports, data inputs",
        "without live Console objects",
    ] {
        assert_eq!(description.contains(instruction), builtin && languages.r);
    }
    for instruction in [
        "An error can leave earlier changes in place",
        "do not resubmit the cell",
        "use only trusted dependencies",
    ] {
        assert!(description.contains(instruction), "{description}");
    }
    assert_eq!(
        description.contains("Switch languages when useful"),
        fields.len() > 1
    );
    assert!(
        properties["control"]["description"]
            .as_str()
            .unwrap()
            .contains("discards")
    );
    assert!(
        properties["requirements"]["description"]
            .as_str()
            .unwrap()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ")
            .contains("not rolled back")
    );
    if !builtin {
        assert!(description.contains("does not supply built-in runtime packages"));
        assert!(description.contains("Managed requirements need"));
    } else if languages.sql {
        assert!(description.contains("CSV, Parquet, JSON, and JSONL directly"));
        assert!(!description.to_ascii_lowercase().contains("provider"));
        let sql = properties["sql"]["description"].as_str().unwrap();
        assert!(sql.contains("selected driver supplies its own SQL"));
        assert!(sql.contains("CLI dot commands are not supported"));
        assert_eq!(sql.contains("With R-owned managed DuckDB"), languages.r);
        if languages.r {
            assert!(sql.contains("DBI"));
        } else {
            assert!(!sql.contains("from an R cell"));
        }
        if languages.python {
            let python = properties["python"]["description"].as_str().unwrap();
            for instruction in [
                "_console.sql_connection(connection)",
                "register(name, frame)",
                "active native Python connection",
            ] {
                assert!(python.contains(instruction), "{python}");
            }
            if languages.r {
                assert!(python.contains("errors if SQL uses R"));
            } else {
                assert!(python.contains("errors if another runtime owns SQL"));
                assert!(python.contains("on Python-owned DuckDB"));
                assert!(!python.contains("`r.name`"));
                assert!(!python.contains("R plot rules"));
            }
        } else {
            assert!(!sql.contains("register(name, frame)"));
        }
        let requirements = properties["requirements"]["properties"]
            .as_object()
            .unwrap();
        for field in ["r", "python", "duckdb"] {
            assert!(requirements.contains_key(field));
        }
    }
}

#[test]
fn configured_language_matrix() {
    for builtin in [true, false] {
        let (baseline, _) = render(Languages::all(), builtin);
        for mask in 1..8 {
            let languages = Languages {
                r: mask & 1 != 0,
                python: mask & 2 != 0,
                sql: mask & 4 != 0,
            };
            let (tool, instructions) = render(languages, builtin);
            let fields = languages.fields();
            let description = tool["description"].as_str().unwrap();
            assert_eq!(instructions, description.split("\n\n").next().unwrap());
            assert!(!instructions.contains('\n') && instructions.len() <= 250);
            for (field, name) in [("r", "R"), ("python", "Python"), ("sql", "SQL")] {
                let visible = fields.contains(&field);
                assert_eq!(
                    instructions
                        .split(|c: char| !c.is_ascii_alphabetic())
                        .any(|word| word == name),
                    visible
                );
            }
            assert!(description.contains(&format!(
                "Send one complete {} cell per call",
                languages.cell_fields()
            )));
            check_guidance(&tool, languages, builtin);

            let mut expected = without_descriptions(&baseline);
            let properties = expected["inputSchema"]["properties"]
                .as_object_mut()
                .unwrap();
            for field in ["r", "python", "sql"] {
                if !fields.contains(&field) {
                    properties.remove(field);
                }
            }
            assert_eq!(without_descriptions(&tool), expected);
        }
    }
}
