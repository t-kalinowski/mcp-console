//! Tool prose derived only from captured launch configuration.
mod requirements;
mod sections;

use std::sync::Arc;

use rmcp::handler::server::router::tool::ToolRouter;
use serde_json::{Map, Value};

use super::ConsoleServer;
use crate::cell::Languages;
use crate::settings::SandboxSettings;

/// Requested interface and preparation mode, never discovered runtime availability.
struct Profile {
    languages: Languages,
    configured_visibility: bool,
    builtin: bool,
}

impl ConsoleServer {
    pub(super) fn configured_tool_router(
        languages: Languages,
        configured_visibility: bool,
        builtin: bool,
        policy: &SandboxSettings,
        no_sandbox: bool,
    ) -> ToolRouter<Self> {
        let profile = Profile {
            languages,
            configured_visibility,
            builtin,
        };
        let mut router = Self::tool_router();
        let send = router
            .map
            .get_mut("send")
            .expect("send tool must be registered");
        send.attr.description = Some(profile.description(policy, no_sandbox).into());
        let schema = Arc::make_mut(&mut send.attr.input_schema);
        let properties = schema
            .get_mut("properties")
            .and_then(Value::as_object_mut)
            .expect("send schema must have object properties");
        let control = properties
            .get_mut("control")
            .and_then(Value::as_object_mut)
            .expect("send control schema must be an object");
        control.insert("type".to_string(), Value::String("string".to_string()));
        if let Some(values) = control.get_mut("enum").and_then(Value::as_array_mut) {
            values.retain(|value| !value.is_null());
        }
        // Omission carries meaning for get/reset. Do not advertise payload defaults.
        for property in properties
            .get_mut("requirements")
            .and_then(|requirements| requirements.get_mut("properties"))
            .and_then(Value::as_object_mut)
            .expect("requirements schema properties")
            .values_mut()
        {
            property
                .as_object_mut()
                .expect("requirement property schema")
                .remove("default");
        }
        // Configured fields stay visible even when an interpreter is unavailable.
        for (field, enabled) in [
            ("r", languages.r),
            ("python", languages.python),
            ("sql", languages.sql),
        ] {
            if !enabled {
                properties.shift_remove(field);
            }
        }
        profile.configure_fields(properties);
        router
    }
}

impl Profile {
    fn restricted_guidance(&self) -> bool {
        self.configured_visibility
            && !(self.languages.r && self.languages.python && self.languages.sql)
    }

    fn multiple_languages(&self) -> bool {
        [self.languages.r, self.languages.python, self.languages.sql]
            .into_iter()
            .filter(|enabled| *enabled)
            .count()
            > 1
    }

    fn description(&self, policy: &SandboxSettings, no_sandbox: bool) -> String {
        // The internal environment filter retains its legacy presentation.
        // Only the public setting selects the new language-specific guidance.
        let described_languages = if self.configured_visibility {
            self.languages
        } else {
            Languages::all()
        };
        let mut description = if !self.builtin {
            let mut scope = if self.restricted_guidance() {
                sections::CUSTOM_SELECTED_SCOPE
            } else {
                sections::CUSTOM_SCOPE
            }
            .to_string();
            if self.multiple_languages() {
                scope.push_str(sections::CUSTOM_SWITCHING);
            }
            scope
        } else if cfg!(windows) && !self.configured_visibility {
            sections::WINDOWS_SCOPE.to_string()
        } else {
            let mut scope = if described_languages.r
                && described_languages.python
                && described_languages.sql
            {
                sections::BUILTIN_SCOPE.to_string()
            } else {
                let names = self
                    .languages
                    .fields()
                    .iter()
                    .map(|field| match *field {
                        "r" => "R",
                        "python" => "Python",
                        "sql" => "SQL",
                        _ => unreachable!(),
                    })
                    .collect::<Vec<_>>()
                    .join(" and ");
                let location = if cfg!(windows) {
                    " for local execution on Windows"
                } else {
                    ""
                };
                format!(
                    "Persistent {names} workbench{location} for exact computation, file and data inspection, transformation, visualization, statistics, simulation, and modeling. State persists across calls."
                )
            };
            if cfg!(windows) {
                scope.push_str(sections::WINDOWS_SELECTED_PREPARATION);
            }
            scope.push_str("\n\n");
            scope.push_str(&self.language_guidance());
            scope.push_str("\n\n");
            scope.push_str(sections::INTERFACE);
            if described_languages.r && described_languages.python {
                scope.push_str(sections::SHARING);
            }
            if described_languages.sql {
                if described_languages.r && described_languages.python {
                    scope.push_str(sections::MANAGED_SQL_SHARING);
                } else {
                    if described_languages.r {
                        scope.push_str(sections::MANAGED_SQL_R);
                    }
                    if described_languages.python {
                        scope.push_str(sections::MANAGED_SQL_PYTHON_SELECTED);
                    }
                    scope.push_str(sections::SQL_PROVIDER_SELECTED);
                }
            }
            scope.push_str(if self.restricted_guidance() && !self.languages.python {
                sections::MANAGED_PREPARATION_SELECTED
            } else {
                sections::MANAGED_PREPARATION
            });
            scope
        };
        description.push_str("\n\nSend one complete ");
        if self.configured_visibility {
            description.push_str(&self.languages.cell_fields());
        } else if cfg!(windows) && self.builtin {
            description.push_str("`r` or `python`");
        } else {
            description.push_str("`r`, `python`, or `sql`");
        }
        description.push_str(sections::SEND_ORDERING);
        description.push_str("\n\n");
        description.push_str(sections::POLLING);
        description.push_str("\n\n");
        description.push_str(sections::OUTPUT);
        description.push_str("\n\n");
        description.push_str(&description_for_launch(policy, no_sandbox));
        description
    }

    fn language_guidance(&self) -> String {
        let mut guidance = String::new();
        for (enabled, section) in [
            (self.languages.sql, sections::SQL_SELECTION),
            (self.languages.r, sections::R_SELECTION),
            (self.languages.python, sections::PYTHON_SELECTION),
        ] {
            if enabled {
                guidance.push_str(section);
            }
        }
        if self.multiple_languages() {
            guidance.push_str(sections::SWITCHING);
        }
        guidance.truncate(guidance.trim_end().len());
        if self.languages.sql {
            guidance.push_str("\n\n");
            guidance.push_str(sections::SQL_FILES);
            guidance.push_str(sections::SQL_DEFAULTS);
            guidance.push_str(sections::SQL_SQLITE);
            guidance.push_str(sections::SQL_EXTENSIONS);
            guidance.push_str(sections::SQL_RESULTS);
        }
        guidance
    }

    fn configure_fields(&self, properties: &mut Map<String, Value>) {
        if self.builtin && self.configured_visibility {
            for (field, description) in [
                ("r", r_description_for(self.languages)),
                ("python", python_description_for(self.languages)),
                ("sql", sql_description_for(self.languages)),
            ] {
                if let Some(property) = properties.get_mut(field) {
                    property["description"] = description.into();
                }
            }
        } else if !self.builtin {
            for (field, section) in [
                ("r", sections::CUSTOM_R),
                ("python", sections::CUSTOM_PYTHON),
                ("sql", sections::CUSTOM_SQL),
            ] {
                if let Some(property) = properties.get_mut(field) {
                    property["description"] = section.into();
                }
            }
        }
        if self.restricted_guidance() {
            properties["stdin"]["description"] =
                format!("{}{}", sections::STDIN_SELECTED, sections::STDIN_ORDERING).into();
            properties["control"]["description"] = control_description_for(false).into();
            properties["timeout_ms"]["description"] = sections::TIMEOUT_SELECTED.into();
            requirements::configure(properties, self.languages, self.builtin);
        }
    }
}

// Schemars uses the same named sections for the ordinary field metadata.
// SQL-only sections are omitted on Windows at construction, never removed by prose matching.
pub(super) fn r_description() -> String {
    r_description_for(Languages::all())
}

fn r_description_for(languages: Languages) -> String {
    let mut description = sections::R_RUNTIME.to_string();
    if languages.python {
        description.push_str(sections::R_BRIDGE);
    }
    if languages.sql {
        description.push_str(sections::R_SQL);
    }
    description.push_str(sections::R_PLOTS);
    description
}

pub(super) fn python_description() -> String {
    python_description_for(Languages::all())
}

fn python_description_for(languages: Languages) -> String {
    let mut description = sections::PYTHON_RUNTIME.to_string();
    if languages.r {
        description.push_str(sections::PYTHON_BRIDGE);
    }
    if languages.sql {
        description.push_str(sections::PYTHON_SQL);
        if languages.r {
            description.push_str(sections::PYTHON_SQL_R);
            description.push_str(sections::PYTHON_SQL_CONNECTION);
        } else {
            description.push_str(sections::PYTHON_SQL_CONNECTION_SELECTED);
        }
    }
    description.push_str(sections::PYTHON_PLOTS);
    if languages.r {
        description.push_str(sections::PYTHON_R_PLOTS);
    }
    description.push_str(sections::PYTHON_END);
    description
}

pub(super) fn sql_description() -> String {
    sql_description_for(Languages::all())
}

fn sql_description_for(languages: Languages) -> String {
    let mut description = sections::SQL_RUNTIME.to_string();
    if languages.r {
        description.push_str(sections::SQL_R_FRAMES);
    }
    if languages.r && languages.python {
        description.push_str(sections::SQL_DRIVERS);
    } else {
        if languages.r {
            description.push_str(sections::SQL_R_DRIVER);
        }
        if languages.python {
            description.push_str(sections::SQL_PYTHON_DRIVER);
        }
        description.push_str(sections::SQL_DIALECT);
    }
    if languages.r {
        description.push_str(sections::SQL_R_STATEMENTS);
    }
    if languages.python {
        description.push_str(if languages.r {
            sections::SQL_PYTHON_FRAMES
        } else {
            sections::SQL_PYTHON_FRAMES_SELECTED
        });
    }
    description.push_str(sections::SQL_OPERATIONS);
    description
}

pub(super) fn control_description() -> String {
    control_description_for(true)
}

fn control_description_for(full: bool) -> String {
    let interrupt = if cfg!(windows) {
        sections::WINDOWS_INTERRUPT
    } else {
        sections::UNIX_INTERRUPT
    };
    format!(
        "{}{interrupt}{}",
        sections::CONTROL_START,
        if full {
            sections::CONTROL_END
        } else {
            sections::CONTROL_END_SELECTED
        }
    )
}

fn description_for_launch(policy: &SandboxSettings, no_sandbox: bool) -> String {
    if no_sandbox {
        return "Evaluated code runs without a sandbox, with the server's permissions, including filesystem and network access. Dependency resolution, when available, may execute installation or build code; use only trusted dependencies.".into();
    }
    let network_access = if policy.contains_key("proxy") {
        "can access the network subject to the launcher's proxy settings"
    } else {
        match policy.get("network").and_then(serde_json::Value::as_str) {
            Some("enabled") => "can directly access the network",
            Some("restricted") => "cannot directly access the network",
            _ => unreachable!("normalized worker network choice"),
        }
    };
    format!(
        "Evaluated code can read host files, {network_access}, and can write in the worker's private temporary directory and to paths explicitly allowed by the launcher. Dependency resolution, when available, uses a separate native resolver sandbox on macOS and Linux with configurable host reads, cache writes, and proxy destinations. Installation or build code may run there; use only trusted dependencies."
    )
}

pub(super) fn stdin_description() -> String {
    format!("{}{}", sections::STDIN_START, sections::STDIN_ORDERING)
}
