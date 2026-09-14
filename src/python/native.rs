use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::{
    Mutex, OnceLock,
    atomic::{AtomicBool, Ordering},
};

const ENVIRONMENT_SOURCE: &str = include_str!("environment.py");
const DISCOVERY_SOURCE: &str = include_str!("discovery.py");
static CONFIGURATION: OnceLock<Configuration> = OnceLock::new();
static INITIALIZED: AtomicBool = AtomicBool::new(false);

struct Configuration {
    selection: Mutex<Selection>,
    temporary: PathBuf,
    thread: std::thread::ThreadId,
    process: u32,
    disabled_reason: String,
}

#[derive(Clone)]
struct Selection {
    python: PathBuf,
    manifest: Option<crate::worker_protocol::PythonRequirementManifest>,
    declared: Option<crate::worker_protocol::PythonRequirementManifest>,
}

pub(super) fn configure(temporary: &Path) -> Result<(), String> {
    let manifest = std::env::var("MCP_CONSOLE_MANAGED_PYTHON")
        .ok()
        .map(|value| serde_json::from_str(&value).map_err(|error| error.to_string()))
        .transpose()?;
    let selected = if manifest.is_some() {
        std::env::var_os("MCP_CONSOLE_PYTHON_EXECUTABLE")
            .ok_or("managed Python has no selected executable")?
    } else {
        std::env::var_os("RETICULATE_PYTHON")
            .filter(|value| !value.is_empty() && value != "managed")
            .unwrap_or_else(|| "python3".into())
    };
    let disabled_reason = match std::env::var("MCP_CONSOLE_EXECUTION_COMPUTE").as_deref() {
        Ok("docker") => {
            "MCP Console dynamic environment resolution is unavailable for Docker targets. Install the distribution in the image and start a new server session."
        }
        Ok("docker_sandbox") => {
            "MCP Console dynamic environment resolution is unavailable for Docker Sandbox targets. Install the distribution in the image and start a new server session."
        }
        _ if std::env::var("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION").as_deref() == Ok("0") => {
            "MCP Console dynamic environment resolution is unavailable. Install the distribution into the ambient Python environment, or install `ir` or `uv` and restart MCP Console."
        }
        _ => {
            "MCP Console is using a user-selected Python environment. Automatic managed package resolution is disabled, and `requirements.python` is also disabled for this interpreter selection. Install the distribution into the selected environment or restart MCP Console with managed Python enabled."
        }
    };
    CONFIGURATION
        .set(Configuration {
            selection: Mutex::new(Selection {
                python: selected.into(),
                manifest,
                declared: None,
            }),
            temporary: temporary.into(),
            thread: std::thread::current().id(),
            process: std::process::id(),
            disabled_reason: disabled_reason.into(),
        })
        .map_err(|_| "Python configuration already captured".to_string())
}

pub(crate) fn ensure_initialized() -> Result<(), String> {
    if INITIALIZED.load(Ordering::SeqCst) {
        return Ok(());
    }
    let configuration = CONFIGURATION
        .get()
        .ok_or("Python configuration is unavailable")?;
    let mut selection = configuration
        .selection
        .lock()
        .map_err(|_| "Python selection lock poisoned")?
        .clone();
    if let Some(candidate) = selection.declared.take() {
        selection.python =
            crate::worker::resolve_python(crate::worker_protocol::PythonResolveRequest {
                requirements: candidate.clone(),
                retained_requirements: candidate.clone(),
                import_resolution: None,
            })?
            .into();
        selection.manifest = Some(candidate);
    }
    let discovered = discover(&selection.python)?;
    let library = discovered["libpython"]
        .as_str()
        .ok_or("Python discovery omitted libpython")?;
    let executable = discovered["executable"]
        .as_str()
        .ok_or("Python discovery omitted executable")?;
    let home = discovered["base_prefix"]
        .as_str()
        .ok_or("Python discovery omitted base_prefix")?;
    super::library::initialize(Path::new(library), executable, home)?;
    let result: Result<(), String> = (|| {
        super::library::install_native()?;
        super::library::install_runtime(super::RUNTIME_SOURCE)?;
        super::library::install_module(c"_mcp_console_environment", ENVIRONMENT_SOURCE)?;
        super::library::call_json(
            c"_mcp_console_environment",
            c"initialize",
            &json!({
                "discovery": discovered,
                "manifest": selection.manifest,
                "temporary": configuration.temporary,
                "disabled_reason": configuration.disabled_reason,
            }),
        )?;
        crate::sql::install_python_runtime()?;
        super::library::call_json(
            c"_mcp_console_sql",
            c"configure",
            &json!({"managed_r": crate::sql::managed_r()}),
        )?;
        super::library::connect_interrupts()?;
        Ok(())
    })();
    super::library::finish_initialization()?;
    result?;
    INITIALIZED.store(true, Ordering::SeqCst);
    *configuration
        .selection
        .lock()
        .map_err(|_| "Python selection lock poisoned")? = selection;
    Ok(())
}

pub(super) fn discover(python: &Path) -> Result<Value, String> {
    let output = std::process::Command::new(python)
        .args(["-c", DISCOVERY_SOURCE])
        .stdin(std::process::Stdio::null())
        .output()
        .map_err(|error| {
            format!(
                "Python is unavailable: cannot run `{}`: {error}",
                python.display()
            )
        })?;
    if !output.status.success() {
        return Err(format!(
            "Python discovery failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("invalid Python discovery: {error}"))
}

pub(super) fn state() -> Result<Value, String> {
    if INITIALIZED.load(Ordering::SeqCst) {
        super::library::call_json(c"_mcp_console_environment", c"state", &Value::Null)
    } else {
        let configuration = CONFIGURATION
            .get()
            .ok_or("Python configuration is unavailable")?;
        let selection = configuration
            .selection
            .lock()
            .map_err(|_| "Python selection lock poisoned")?;
        Ok(
            json!({"manifest": selection.declared.as_ref().or(selection.manifest.as_ref()), "discovery": null}),
        )
    }
}

pub(super) fn declare(request: Value) -> Result<Value, String> {
    if INITIALIZED.load(Ordering::SeqCst) {
        return prepare(request);
    }
    let candidate = serde_json::from_value::<crate::worker_protocol::PythonRequirementManifest>(
        request["manifest"].clone(),
    )
    .map_err(|error| error.to_string())?
    .normalized();
    crate::python_requirement::validate_all(&candidate.packages)?;
    crate::python_requirement::validate_version_constraints(&candidate.python_version)?;
    let configuration = CONFIGURATION
        .get()
        .ok_or("Python configuration is unavailable")?;
    configuration
        .selection
        .lock()
        .map_err(|_| "Python selection lock poisoned")?
        .declared = Some(candidate);
    Ok(json!({"kind": "prepared"}))
}

pub(super) fn prepare(request: Value) -> Result<Value, String> {
    if INITIALIZED.load(Ordering::SeqCst) {
        return super::library::call_json(c"_mcp_console_environment", c"prepare", &request);
    }
    // Before CPython activation, the same Console manifest can select another
    // interpreter. Resolving a requirement is not interpreter initialization.
    let result = (|| {
        let configuration = CONFIGURATION
            .get()
            .ok_or("Python configuration is unavailable")?;
        let selection = configuration
            .selection
            .lock()
            .map_err(|_| "Python selection lock poisoned")?
            .clone();
        let current = selection
            .declared
            .as_ref()
            .or(selection.manifest.as_ref())
            .ok_or_else(|| configuration.disabled_reason.clone())?;
        let candidate = if let Some(manifest) = request.get("manifest") {
            serde_json::from_value::<crate::worker_protocol::PythonRequirementManifest>(
                manifest.clone(),
            )
            .map_err(|error| error.to_string())?
        } else {
            let mut candidate = current.clone();
            candidate.packages.extend(
                serde_json::from_value::<Vec<String>>(request["packages"].clone())
                    .map_err(|error| error.to_string())?,
            );
            candidate
        }
        .normalized();
        if Some(&candidate) == selection.manifest.as_ref() {
            return Ok(());
        }
        let python = crate::worker::resolve_python(crate::worker_protocol::PythonResolveRequest {
            requirements: candidate.clone(),
            retained_requirements: candidate.clone(),
            import_resolution: None,
        })?;
        discover(Path::new(&python))?;
        *configuration
            .selection
            .lock()
            .map_err(|_| "Python selection lock poisoned")? = Selection {
            python: python.into(),
            manifest: Some(candidate.clone()),
            declared: None,
        };
        crate::worker::publish_python_activation(candidate)
    })();
    Ok(match result {
        Ok(()) => json!({"kind": "prepared"}),
        Err(message) => json!({"kind": "failed", "message": message}),
    })
}

pub(super) fn call(request: Value) -> Result<Value, String> {
    let configuration = CONFIGURATION
        .get()
        .ok_or("Python configuration is unavailable")?;
    if configuration.process != std::process::id()
        || configuration.thread != std::thread::current().id()
    {
        return Err("Console worker services are available only on the main worker thread".into());
    }
    let payload = &request["payload"];
    match request["operation"]
        .as_str()
        .ok_or("missing native operation")?
    {
        "interrupt" => Ok(Value::Bool(crate::worker::interrupt::take_python())),
        "input" => crate::worker::read_line(payload.as_str().ok_or("invalid input prompt")?)
            .map(Value::String),
        "output" => {
            let channel = match payload["channel"].as_str() {
                Some("output") => crate::worker_protocol::ConsoleChannel::Output,
                Some("diagnostic") => crate::worker_protocol::ConsoleChannel::Diagnostic,
                _ => return Err("invalid output channel".into()),
            };
            crate::worker::emit_output(
                channel,
                payload["text"]
                    .as_str()
                    .ok_or("invalid output text")?
                    .as_bytes(),
            );
            Ok(Value::Null)
        }
        "plot" => {
            crate::worker::publish_plot(Ok(payload.as_str().ok_or("invalid plot")?.to_string()));
            Ok(Value::Null)
        }
        "resolve_python" => crate::worker::resolve_python(
            serde_json::from_value(payload.clone()).map_err(|error| error.to_string())?,
        )
        .map(Value::String),
        "resolve_version" => crate::worker::resolve_python_version(
            serde_json::from_value(payload.clone()).map_err(|error| error.to_string())?,
        )
        .map(Value::String),
        "activate_python" => {
            crate::worker::publish_python_activation(
                serde_json::from_value(payload.clone()).map_err(|error| error.to_string())?,
            )?;
            Ok(Value::Null)
        }
        "discover_python" => discover(Path::new(payload.as_str().ok_or("invalid interpreter")?)),
        "interop" => {
            crate::worker::activate_r()?;
            super::reticulate::attach()?;
            Ok(Value::Null)
        }
        _ => Err("unknown Console native operation".into()),
    }
}
