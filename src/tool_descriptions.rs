use std::borrow::Cow;
use std::sync::Arc;

use rmcp::handler::server::tool::ToolRouter;
use rmcp::model::{JsonObject, Tool};
use serde_json::Value;

const PYTHON_ENVIRONMENT: &str = "{{python_environment}}";
const PACKAGE_GUIDANCE: &str = "{{package_guidance}}";
const PYTHON_DETAILS: &str = "{{python_details}}";
const PYTHON_RESOLUTION: &str = "{{python_resolution}}";
const LIVE_PREPARATION: &str = "{{live_preparation}}";

#[derive(Clone, Copy)]
pub(crate) enum WorkerProfile {
    BuiltIn { initially_managed_python: bool },
    Custom,
    Unsupported,
}

struct BuiltInValues {
    python_environment: &'static str,
    send_package_guidance: &'static str,
    python_details: &'static str,
    session_package_guidance: &'static str,
    python_resolution: &'static str,
    live_preparation: &'static str,
    session_properties: &'static [PropertyDescription],
}

const MANAGED_PYTHON: BuiltInValues = BuiltInValues {
    python_environment: "The built-in managed Python environment includes NumPy and pandas.",
    send_package_guidance: "Do not probe package availability in cells. If you want to use a package, prepare it with `session`, then load it directly with R `library()` or Python `import`.",
    python_details: "The built-in managed Python environment includes NumPy and pandas. Use\n`session` to prepare other packages such as scikit-learn or Matplotlib.",
    session_package_guidance: "Do not probe package availability in cells. If you want to use a package, use `prepare`, then load it with R `library()` or Python `import` in `send`.",
    python_resolution: " Managed Python requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements.",
    live_preparation: "An idle server-managed worker can add R and compatible Python requirements without losing live state.",
    session_properties: &[],
};

const CONFIGURED_PYTHON: BuiltInValues = BuiltInValues {
    python_environment: "Python initially follows inherited `RETICULATE_PYTHON` configuration. A successful `prepare` with Python requirements before the worker starts or `restart` with Python requirements after it starts replaces it with a managed environment. Packages in the active Python environment are not discovered or advertised.",
    send_package_guidance: "Do not probe package availability in cells. Load R packages directly with `library()`. Import packages provided by the active Python environment directly. If you want to use an additional package, prepare it with `session`. If Python preparation reports `[restart required]`, call `session` with `action = \"restart\"` and those Python requirements instead; restart loses worker state.",
    python_details: "Python initially follows inherited `RETICULATE_PYTHON` configuration.\nA successful `prepare` with Python requirements before the worker starts or `restart` with Python requirements after it starts replaces it with a managed environment.\nPackages in the active Python environment are not discovered or advertised. Import packages provided by the active Python environment directly. If you want to use an additional Python package, prepare it with `session`. If Python preparation reports `[restart required]`, call `session` with `action = \"restart\"` and that Python requirement instead; restart loses worker state.",
    session_package_guidance: "Do not probe package availability in cells. Load packages provided by the active Python environment directly with `import`, and load R packages with `library()`. If you want to use an additional package, use `prepare`. If Python preparation reports `[restart required]`, call `restart` with those Python requirements instead; restart loses worker state.",
    python_resolution: " When managed Python is active, requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements.",
    live_preparation: "An idle worker can add R requirements without losing live state. Once Python is managed, it may also activate compatible Python additions without losing live state.",
    session_properties: CONFIGURED_SESSION_PROPERTIES,
};

struct PropertyDescription {
    path: &'static [&'static str],
    description: &'static str,
}

struct ToolDescription {
    name: &'static str,
    description: &'static str,
    properties: &'static [PropertyDescription],
}

const CONFIGURED_SESSION_PROPERTIES: &[PropertyDescription] = &[
    PropertyDescription {
        path: &["properties", "action"],
        description: "`prepare` adds R or Python requirements before a worker starts. An idle worker can add R requirements without losing state. Once Python is managed, it may also activate compatible Python additions without losing state. After an inherited Python worker starts, use `restart` with Python requirements; restart loses worker state and starts its replacement.",
    },
    PropertyDescription {
        path: &["properties", "requirements", "properties", "python"],
        description: "Additive, single-line PEP 508 requirements for `prepare` or `restart`, for example `polars>=1`, `scikit-learn`, or `matplotlib`. Before the first worker starts, `prepare` can select managed Python. After an inherited Python worker starts, supply additions to `restart`. Once Python is managed, an idle worker may activate compatible `prepare` additions without losing state.",
    },
];

const CUSTOM_SEND_PROPERTIES: &[PropertyDescription] = &[
    PropertyDescription {
        path: &["properties", "r"],
        description: "Complete multiline cell sent to the custom worker with the `r` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll.",
    },
    PropertyDescription {
        path: &["properties", "python"],
        description: "Complete multiline cell sent to the custom worker with the `python` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll.",
    },
    PropertyDescription {
        path: &["properties", "sql"],
        description: "Complete multiline cell sent to the custom worker with the `sql` language tag. The worker defines how the source is evaluated and returned. Omit to send stdin or poll.",
    },
    PropertyDescription {
        path: &["properties", "stdin"],
        description: "Text queued exactly as UTF-8 bytes to custom worker standard input; no newline is added. Send it with a cell to prequeue input or on its own while the worker is running or idle. If output ends in `[stdin needed]`, send the requested input here. Unread text can satisfy later reads and is discarded by restart.",
    },
];

const CUSTOM_SESSION_PROPERTIES: &[PropertyDescription] = &[PropertyDescription {
    path: &["properties", "action"],
    description: "`restart` replaces the custom worker and starts it if needed.",
}];

const CUSTOM_DESCRIPTIONS: &[ToolDescription] = &[
    ToolDescription {
        name: "send",
        description: "Persistent console backed by the custom worker selected with `serve --worker`. MCP Console passes one complete cell tagged `r`, `python`, or `sql`; the custom worker defines the supported languages and installed packages, together with its state model, interoperability, plotting, and ordinary error behavior. Package availability and loading are worker-defined; package preparation with `session` is unavailable. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` to queue exact UTF-8 text to worker standard input without adding a newline; omit code and stdin to poll. A wait timeout does not stop the worker operation, and running work must be collected before new code is sent. The worker can read host files but cannot directly access the network and can write only within its private temporary directory.",
        properties: CUSTOM_SEND_PROPERTIES,
    },
    ToolDescription {
        name: "session",
        description: "Restart the persistent custom-worker session. Call `session` with `action = \"restart\"` and omit `requirements`; package preparation and restart-time requirements are unavailable with a custom worker. Restart replaces the worker, starts it if needed, and loses all worker-owned state and unread stdin.",
        properties: CUSTOM_SESSION_PROPERTIES,
    },
];

const UNSUPPORTED_SEND_PROPERTIES: &[PropertyDescription] = &[
    PropertyDescription {
        path: &["properties", "r"],
        description: "R cells cannot be evaluated because MCP Console workers are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "python"],
        description: "Python cells cannot be evaluated because MCP Console workers are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "sql"],
        description: "SQL cells cannot be evaluated because MCP Console workers are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "stdin"],
        description: "Worker stdin is unavailable because MCP Console workers are currently supported only on macOS.",
    },
];

const UNSUPPORTED_SESSION_PROPERTIES: &[PropertyDescription] = &[
    PropertyDescription {
        path: &["properties", "action"],
        description: "`prepare` and `restart` are unavailable because MCP Console workers are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "requirements"],
        description: "Requirement preparation is unavailable because managed package environments are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "requirements", "properties", "r"],
        description: "Managed R libraries are currently supported only on macOS.",
    },
    PropertyDescription {
        path: &["properties", "requirements", "properties", "python"],
        description: "Managed Python environments are currently supported only on macOS.",
    },
];

const UNSUPPORTED_DESCRIPTIONS: &[ToolDescription] = &[
    ToolDescription {
        name: "send",
        description: "Console execution and worker stdin are unavailable on this operating system because MCP Console workers are currently supported only on macOS. Calls that submit code or nonempty stdin fail; calls without them can still poll server state.",
        properties: UNSUPPORTED_SEND_PROPERTIES,
    },
    ToolDescription {
        name: "session",
        description: "Console sessions are unavailable on this operating system because MCP Console workers and managed package environments are currently supported only on macOS. Calls to `session` cannot prepare requirements or restart a worker.",
        properties: UNSUPPORTED_SESSION_PROPERTIES,
    },
];

pub(crate) fn render<S>(mut tools: ToolRouter<S>, profile: WorkerProfile) -> ToolRouter<S> {
    match profile {
        WorkerProfile::BuiltIn {
            initially_managed_python,
        } => render_built_in(&mut tools, initially_managed_python),
        WorkerProfile::Custom => render_custom(&mut tools),
        WorkerProfile::Unsupported => render_replacements(&mut tools, UNSUPPORTED_DESCRIPTIONS),
    }
    tools
}

fn render_built_in<S>(tools: &mut ToolRouter<S>, managed_python: bool) {
    let values = if managed_python {
        &MANAGED_PYTHON
    } else {
        &CONFIGURED_PYTHON
    };
    let send = registered_tool(tools, "send");
    render_tool_description(
        send,
        &[
            (PYTHON_ENVIRONMENT, values.python_environment),
            (PACKAGE_GUIDANCE, values.send_package_guidance),
            (PYTHON_RESOLUTION, values.python_resolution),
        ],
    );
    render_property_description(
        send,
        &["properties", "python"],
        &[(PYTHON_DETAILS, values.python_details)],
    );
    let session = registered_tool(tools, "session");
    render_tool_description(
        session,
        &[
            (PACKAGE_GUIDANCE, values.session_package_guidance),
            (LIVE_PREPARATION, values.live_preparation),
        ],
    );
    for property in values.session_properties {
        set_property_description(session, property.path, property.description);
    }
}

fn render_custom<S>(tools: &mut ToolRouter<S>) {
    render_replacements(tools, CUSTOM_DESCRIPTIONS);

    let session = registered_tool(tools, "session");
    let properties = schema_object_at(Arc::make_mut(&mut session.input_schema), &["properties"]);
    let action = properties
        .get_mut("action")
        .and_then(Value::as_object_mut)
        .expect("session action schema should be an object");
    action.insert(
        "enum".to_string(),
        Value::Array(vec![Value::String("restart".to_string())]),
    );
    properties
        .remove("requirements")
        .expect("session requirements schema should exist");
}

fn render_replacements<S>(tools: &mut ToolRouter<S>, descriptions: &[ToolDescription]) {
    for description in descriptions {
        let tool = registered_tool(tools, description.name);
        tool.description = Some(Cow::Borrowed(description.description));
        for property in description.properties {
            set_property_description(tool, property.path, property.description);
        }
    }
}

fn registered_tool<'a, S>(tools: &'a mut ToolRouter<S>, name: &str) -> &'a mut Tool {
    &mut tools
        .map
        .get_mut(name)
        .expect("description should name a registered tool")
        .attr
}

fn render_tool_description(tool: &mut Tool, values: &[(&str, &str)]) {
    let template = tool
        .description
        .take()
        .expect("registered tool should have a description");
    tool.description = Some(Cow::Owned(render_text(&template, values)));
}

fn render_property_description(tool: &mut Tool, path: &[&str], values: &[(&str, &str)]) {
    let property = schema_object_at(Arc::make_mut(&mut tool.input_schema), path);
    let template = property
        .get("description")
        .and_then(Value::as_str)
        .expect("tool property should have a description");
    property.insert(
        "description".to_string(),
        Value::String(render_text(template, values)),
    );
}

fn set_property_description(tool: &mut Tool, path: &[&str], description: &str) {
    schema_object_at(Arc::make_mut(&mut tool.input_schema), path).insert(
        "description".to_string(),
        Value::String(description.to_string()),
    );
}

fn render_text(template: &str, values: &[(&str, &str)]) -> String {
    let mut rendered = template.to_string();
    for (placeholder, value) in values {
        assert!(
            rendered.contains(placeholder),
            "tool description should contain {placeholder}"
        );
        rendered = rendered.replace(placeholder, value);
    }
    assert!(
        !rendered.contains("{{"),
        "tool description should not contain an unresolved placeholder"
    );
    rendered
}

fn schema_object_at<'a>(schema: &'a mut JsonObject, path: &[&str]) -> &'a mut JsonObject {
    let (first, rest) = path
        .split_first()
        .expect("tool description schema path should not be empty");
    let mut node = schema
        .get_mut(*first)
        .expect("tool description schema path should exist");
    for field in rest {
        node = node
            .as_object_mut()
            .and_then(|object| object.get_mut(*field))
            .expect("tool description schema path should exist");
    }
    node.as_object_mut()
        .expect("tool description schema path should name an object")
}
