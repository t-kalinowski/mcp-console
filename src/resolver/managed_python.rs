use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::Serialize;

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, completed_write, resolver_command,
};

const PYTHON_PATH_SOURCE: &str = r#"
import sys

with open(sys.argv[-1], "w", encoding="utf-8") as stream:
    stream.write(sys.executable)
"#;

#[derive(Clone, serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ManagedPython {
    python: PathBuf,
    requirements: crate::worker_protocol::PythonRequirementManifest,
    #[serde(default)]
    native: Option<Box<crate::python::NativePython>>,
}

#[derive(Serialize)]
struct ResolverInput<'a> {
    python: &'a str,
    packages: Vec<&'a str>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    python_version: Vec<&'a str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    exclude_newer: Option<&'a str>,
}

impl ManagedPython {
    pub(crate) fn native(&self) -> Option<&crate::python::NativePython> {
        self.native.as_deref()
    }
    pub(crate) fn set_native(&mut self, native: crate::python::NativePython) {
        self.native = Some(Box::new(native));
    }

    pub(crate) fn configure_worker(&self, command: &mut Command) {
        command.env("RETICULATE_PYTHON", "managed");
        command.env(
            "MCP_CONSOLE_MANAGED_PYTHON",
            serde_json::to_string(&self.requirements)
                .expect("managed Python requirements should serialize as JSON"),
        );
    }

    pub(crate) fn python(&self) -> &Path {
        &self.python
    }

    pub(crate) fn requirements(&self) -> &crate::worker_protocol::PythonRequirementManifest {
        &self.requirements
    }

    pub(crate) fn with_retained_requirements(
        mut self,
        requirements: crate::worker_protocol::PythonRequirementManifest,
    ) -> Self {
        self.requirements = requirements;
        self
    }
}

pub(crate) fn resolve_python_manifest(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    crate::python_requirement::validate_all(&requirements.packages)?;
    crate::python_requirement::validate_version_constraints(&requirements.python_version)?;
    let requirements = requirements.normalized();
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let versions = resolve_python_versions_with(configuration, &resolver, &mut on_started)?;
    let resolved_python = versions
        .resolve(&requirements.python_version)
        .map_err(|error| format!("managed Python version resolution failed: {}", error.trim()))?;
    let output_path = super::result_file::ResultFile::create(&std::env::temp_dir())?;
    let output = run_managed_python_resolver(
        &requirements,
        &resolved_python,
        output_path.path(),
        configuration,
        &resolver,
        &mut on_started,
    )?;
    if !output.status.success() {
        let error = resolver_error(&output);
        let python = if requirements.python_version.is_empty() {
            format!("{resolved_python} (reticulate default)")
        } else {
            requirements.python_version.join(", ")
        };
        let packages = requirements.packages.iter().map(String::as_str).collect();
        let python_version = requirements
            .python_version
            .iter()
            .map(String::as_str)
            .collect();
        let input = serde_json::to_string_pretty(&ResolverInput {
            python: &python,
            packages,
            python_version,
            exclude_newer: requirements.exclude_newer.as_deref(),
        })
        .expect("resolver input strings should serialize as JSON");
        return Err(format!(
            "managed Python resolution failed:
resolver input:
{input}
uv output:
{error}"
        ));
    }
    check_resolver_control(&resolver, "managed Python resolution")?;
    let bytes = output_path.read(1024 * 1024)?;
    let text = String::from_utf8(bytes).map_err(|_| "managed Python returned a non-UTF-8 path")?;
    let python = PathBuf::from(text.trim());
    if !python.is_absolute() || !python.is_file() {
        return Err(format!(
            "managed Python returned invalid interpreter {}",
            python.display()
        ));
    }
    warm_matplotlib(&python, &resolver, &mut on_started)?;
    Ok(ManagedPython {
        python,
        native: None,
        requirements,
    })
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    crate::python_requirement::validate_version_constraints(&constraints)?;
    let versions = resolve_python_versions(configuration, on_started)?;
    versions
        .resolve(&constraints)
        .map_err(|error| format!("managed Python version resolution failed: {}", error.trim()))
}

fn resolve_python_versions<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: F,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    resolve_python_versions_with(configuration, &resolver, &mut on_started)
}

fn resolve_python_versions_with<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let configured_preference = configuration.python_preference();
    let managed = OsStr::new("only-managed");
    let system = OsStr::new("only-system");
    match configured_preference {
        None => {
            let versions = run_uv_python_list(configuration, resolver, on_started, managed, true)?;
            if versions.is_empty() {
                return Ok(
                    run_uv_python_list(configuration, resolver, on_started, system, false)?
                        .rank(false),
                );
            }
            Ok(versions.rank(true))
        }
        Some(preference) if preference == OsStr::new("managed") => {
            let mut versions =
                run_uv_python_list(configuration, resolver, on_started, managed, true)?;
            versions.extend(run_uv_python_list(
                configuration,
                resolver,
                on_started,
                system,
                false,
            )?);
            Ok(versions.rank(true))
        }
        Some(preference) if preference == OsStr::new("system") => {
            let mut versions =
                run_uv_python_list(configuration, resolver, on_started, managed, true)?;
            versions.extend(run_uv_python_list(
                configuration,
                resolver,
                on_started,
                system,
                false,
            )?);
            Ok(versions.rank(false))
        }
        Some(preference) => {
            let prefer_managed = preference != system;
            Ok(run_uv_python_list(
                configuration,
                resolver,
                on_started,
                preference,
                prefer_managed,
            )?
            .rank(prefer_managed))
        }
    }
}

fn run_uv_python_list<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
    preference: &OsStr,
    managed: bool,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let uv = configuration.uv()?;
    let program = Path::new(uv);
    let mut command = resolver_command(program);
    command
        .args([
            "python",
            "list",
            "--all-versions",
            "--color",
            "never",
            "--output-format",
            "json",
            "--python-preference",
        ])
        .arg(preference)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("VIRTUAL_ENV");
    configuration.configure_direct(&mut command)?;
    let output = run_resolver_command(
        command,
        resolver,
        on_started,
        program,
        "managed Python version",
    )?;
    if !output.status.success() {
        return Err(format!(
            "managed Python version resolution failed with {}: {}",
            output.status,
            resolver_error(&output)
        ));
    }
    check_resolver_control(resolver, "managed Python version resolution")?;
    super::python_version::PythonVersions::parse(&output.stdout, managed).map_err(|error| {
        format!("managed Python version resolver returned invalid output: {error}")
    })
}

fn run_managed_python_resolver<F>(
    requirements: &crate::worker_protocol::PythonRequirementManifest,
    resolved_python: &str,
    output_path: &Path,
    configuration: &super::ManagedPythonResolverConfiguration,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<ResolverOutput, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let uv = configuration.uv()?;
    let program = Path::new(uv);
    let mut command = resolver_command(program);
    command
        .args(["tool", "run", "--isolated", "--python"])
        .arg(resolved_python);
    if let Some(exclude_newer) = requirements.exclude_newer.as_deref() {
        command.args(["--exclude-newer", exclude_newer]);
    }
    for package in &requirements.packages {
        command.arg("--with").arg(package);
    }
    command
        .args(["--", "python", "-c", PYTHON_PATH_SOURCE])
        .arg(output_path)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_remove("VIRTUAL_ENV");
    configuration.configure_direct(&mut command)?;
    // A no-cache tool environment is deleted when `uv tool run` exits,
    // so it cannot back a retained MCP Console session.
    command.env_remove("UV_NO_CACHE");
    run_resolver_command(command, resolver, on_started, program, "managed Python")
}

fn warm_matplotlib<F>(
    python: &Path,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
) -> Result<(), String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let mut command = resolver_command(python);
    command
        .args(["-I", "-c", "import matplotlib.font_manager"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    run_resolver_command(
        command,
        resolver,
        on_started,
        python,
        "managed Python cache warmup",
    )?;
    check_resolver_control(resolver, "managed Python cache warmup")?;
    Ok(())
}

fn check_resolver_control(resolver: &ResolverProcess, operation: &str) -> Result<(), String> {
    match resolver.stop_handle().control_outcome() {
        Some(super::ResolverControlOutcome::Interrupted) => Err(format!("{operation} interrupted")),
        Some(super::ResolverControlOutcome::Cancelled) => Err(format!("{operation} cancelled")),
        None => Ok(()),
    }
}

fn resolver_error(output: &ResolverOutput) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stderr = stderr.trim();
    if !stderr.is_empty() {
        return stderr.to_string();
    }
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn run_resolver_command<F>(
    mut command: Command,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let (mut child, stdout, stderr) = resolver.spawn(&mut command).map_err(|error| {
        format!(
            "failed to run {kind} resolver with `{}`: {error}",
            program.display()
        )
    })?;
    if let Some(on_started) = on_started.take()
        && let Err(error) = on_started(resolver.stop_handle())
    {
        resolver
            .abort(&mut child, program, kind)
            .map_err(|cleanup| format!("{error}; {cleanup}"))?;
        return Err(error);
    }
    resolver.wait(&mut child, completed_write(), stdout, stderr, program, kind)
}
