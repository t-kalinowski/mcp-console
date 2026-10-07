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
            sections::CUSTOM_SCOPE.to_string()
        } else if cfg!(windows) && !self.configured_visibility {
            sections::WINDOWS_SCOPE.to_string()
        } else {
            if described_languages.r && described_languages.python && described_languages.sql {
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
                    "Persistent {names} workbench{location} for calculations, data analysis, and plots."
                )
            }
        };
        description.push_str(" Send one complete ");
        if self.configured_visibility {
            description.push_str(&self.languages.cell_fields());
        } else if cfg!(windows) && self.builtin {
            description.push_str("`r` or `python`");
        } else {
            description.push_str("`r`, `python`, or `sql`");
        }
        description.push_str(sections::SEND_WORKFLOW);
        if self.builtin {
            description.push_str(sections::DISPLAY);
            if !cfg!(windows) || self.configured_visibility {
                description.push_str("\n\n");
                description.push_str(&self.language_guidance());
            }
        } else {
            description.push_str("\n\n");
            description.push_str(if self.restricted_guidance() {
                sections::CUSTOM_SELECTED_CAPABILITIES
            } else {
                sections::CUSTOM_CAPABILITIES
            });
            if self.multiple_languages() {
                description.push_str(sections::CUSTOM_SWITCHING);
            }
        }
        description.push_str("\n\n");
        description.push_str(sections::SEND_ORDERING);
        description.push(' ');
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
            guidance.truncate(guidance.trim_end().len());
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
        }
        requirements::configure(
            properties,
            if self.configured_visibility {
                self.languages
            } else {
                Languages::all()
            },
            self.builtin,
        );
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
    if languages.r {
        description.push_str(sections::SQL_R_STATEMENTS);
    }
    description.push_str(sections::SQL_OPERATIONS);
    description
}

pub(super) fn control_description() -> String {
    control_description_for(true)
}

fn control_description_for(full: bool) -> String {
    format!(
        "{}{}{}",
        sections::CONTROL_START,
        sections::INTERRUPT,
        if full {
            sections::CONTROL_END
        } else {
            sections::CONTROL_END_SELECTED
        }
    )
}

fn description_for_launch(policy: &SandboxSettings, no_sandbox: bool) -> String {
    let profile = policy.get("extends").and_then(serde_json::Value::as_str);
    let filesystem = policy
        .get("filesystem")
        .and_then(|filesystem| filesystem.get("kind"))
        .and_then(crate::settings::native_variant_name)
        .or_else(|| {
            (!policy.contains_key("filesystem") && profile.is_some()).then_some("restricted")
        });
    let network = policy
        .get("network")
        .and_then(crate::settings::native_variant_name)
        .or_else(|| (!policy.contains_key("network") && profile.is_some()).then_some("restricted"));
    let network_access = match (filesystem, network, policy.get("proxy")) {
        // The pinned runner enforces managed proxy routing even with network enabled.
        (_, _, Some(proxy)) if !proxy.is_null() => {
            "can access the network subject to the launcher's proxy settings"
        }
        (Some("restricted" | "unrestricted"), Some("enabled"), _) => {
            "can directly access the network"
        }
        (Some("restricted" | "unrestricted"), Some("restricted"), _) => {
            "cannot directly access the network"
        }
        _ => "has network access governed by the launcher's sandbox settings",
    };
    let mut writable = vec![if cfg!(windows) {
        "private `TMPDIR` (also `TEMP` and `TMP`)".to_string()
    } else {
        "private `TMPDIR`".to_string()
    }];
    if profile == Some(":workspace")
        && let Some(workspace) = policy.get("workspace")
    {
        writable.push(format!("`{workspace}`"));
    }
    if let Some(entries) = policy
        .get("filesystem")
        .and_then(|filesystem| filesystem.get("entries"))
        .and_then(Value::as_array)
    {
        for entry in entries {
            if entry
                .get("access")
                .and_then(crate::settings::native_variant_name)
                == Some("write")
                && let Some(path) = entry.get("path")
            {
                let location = match (path.get("type").and_then(Value::as_str), path.get("path")) {
                    (Some("path"), Some(location)) => location,
                    _ => path,
                };
                writable.push(format!("`{location}`"));
            }
        }
    }
    let writable = format!(
        "Writable locations: {}; subject to more specific read/deny rules",
        writable.join(", ")
    );
    let sandbox_access = match filesystem {
        Some("restricted") if profile == Some(":workspace") => format!(
            "uses the native \":workspace\" profile and {network_access}. {writable}. Workspace .git, .agents, .codex, and .claude paths are readable and protected from writes by default; explicit rules can override these defaults or restrict reads"
        ),
        Some("restricted") if profile == Some(":read-only") => format!(
            "uses the native \":read-only\" profile: it can read host files subject to configured restrictions and {network_access}. {writable}"
        ),
        Some("restricted") => format!("can read host files and {network_access}. {writable}"),
        Some("unrestricted") => {
            format!("has unrestricted filesystem access and {network_access}")
        }
        _ => format!(
            "has filesystem access governed by the launcher's sandbox settings and {network_access}"
        ),
    };

    if no_sandbox {
        "Evaluated code runs without a sandbox, with the server's permissions, including filesystem and network access. Package preparation may execute installation or build code; use only trusted dependencies.".into()
    } else {
        format!(
            "Evaluated code {sandbox_access}. Package preparation may execute installation or build code with separate filesystem and network permissions; use only trusted dependencies."
        )
    }
}

pub(super) fn stdin_description() -> String {
    format!("{}{}", sections::STDIN_START, sections::STDIN_ORDERING)
}
