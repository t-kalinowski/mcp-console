//! Tool prose derived only from captured launch configuration.
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
    builtin: bool,
}

impl ConsoleServer {
    pub(super) fn configured_tool_router(
        languages: Languages,
        builtin: bool,
        policy: &SandboxSettings,
        no_sandbox: bool,
    ) -> ToolRouter<Self> {
        let profile = Profile { languages, builtin };
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
    fn multiple_languages(&self) -> bool {
        [self.languages.r, self.languages.python, self.languages.sql]
            .into_iter()
            .filter(|enabled| *enabled)
            .count()
            > 1
    }

    fn description(&self, policy: &SandboxSettings, no_sandbox: bool) -> String {
        let mut description = if !self.builtin {
            let mut scope = sections::CUSTOM_SCOPE.to_string();
            if self.multiple_languages() {
                scope.push_str(sections::CUSTOM_SWITCHING);
            }
            scope
        } else if cfg!(windows) {
            sections::WINDOWS_SCOPE.to_string()
        } else {
            let mut scope = sections::BUILTIN_SCOPE.to_string();
            scope.push_str("\n\n");
            scope.push_str(&self.language_guidance());
            scope.push_str("\n\n");
            scope.push_str(sections::SHARING);
            scope.push_str(sections::MANAGED_SQL_SHARING);
            scope.push_str(sections::MANAGED_PREPARATION);
            scope
        };
        description.push_str("\n\nSend one complete ");
        description.push_str(if self.builtin && cfg!(windows) {
            sections::WINDOWS_CELL_FIELDS
        } else {
            sections::CELL_FIELDS
        });
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
        if !self.builtin {
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
    }
}

// Schemars uses the same named sections for the ordinary field metadata.
// SQL-only sections are omitted on Windows at construction, never removed by prose matching.
pub(super) fn r_description() -> String {
    let mut description = sections::R_RUNTIME.to_string();
    if !cfg!(windows) {
        description.push_str(sections::R_SQL);
    }
    description.push_str(sections::R_PLOTS);
    description
}

pub(super) fn python_description() -> String {
    let mut description = sections::PYTHON_RUNTIME.to_string();
    if !cfg!(windows) {
        description.push_str(sections::PYTHON_SQL);
    }
    description.push_str(sections::PYTHON_PLOTS);
    description
}

pub(super) fn control_description() -> String {
    let interrupt = if cfg!(windows) {
        sections::WINDOWS_INTERRUPT
    } else {
        sections::UNIX_INTERRUPT
    };
    format!(
        "{}{interrupt}{}",
        sections::CONTROL_START,
        sections::CONTROL_END
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
    let sandbox_access = match filesystem {
        Some("restricted") if profile == Some(":workspace") => format!(
            "uses the native \":workspace\" profile: it can edit files beneath the fixed launch workspace, write in the worker's private temporary directory and to explicitly allowed paths, and {network_access}. The workspace's .git, .agents, .codex, and .claude paths are readable and protected from writes by default. Explicit native rules can override these defaults or restrict reads"
        ),
        Some("restricted") if profile == Some(":read-only") => format!(
            "uses the native \":read-only\" profile: it can read host files subject to configured read restrictions, write in the worker's private temporary directory and to explicitly allowed paths, and {network_access}"
        ),
        Some("restricted") => format!(
            "can read host files, {network_access}, and can write in the worker's private temporary directory and to paths explicitly allowed by the launcher"
        ),
        Some("unrestricted") => {
            format!("has unrestricted filesystem access and {network_access}")
        }
        _ => format!(
            "has filesystem access governed by the launcher's sandbox settings and {network_access}"
        ),
    };

    if no_sandbox {
        "Evaluated code runs without a sandbox, with the server's permissions, including filesystem and network access. Dependency resolution, when available, may execute installation or build code; use only trusted dependencies.".into()
    } else {
        format!(
            "Evaluated code {sandbox_access}. Dependency resolution, when available, uses a separate native resolver sandbox on macOS and Linux with configurable host reads, cache writes, and proxy destinations. Installation or build code may run there; use only trusted dependencies."
        )
    }
}
