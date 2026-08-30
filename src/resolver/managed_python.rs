use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::path::{Path, PathBuf};
use std::process::{self, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::Serialize;

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, completed_write, read_output,
    resolver_command, stop_resolver,
};

const PYTHON_PATH_SOURCE: &str = r#"
import sys

with open(sys.argv[-1], "w", encoding="utf-8") as stream:
    stream.write(sys.executable)
"#;
static PYTHON_PATH_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone)]
pub(crate) struct ManagedPython {
    python: PathBuf,
    requirements: crate::worker_protocol::PythonRequirementManifest,
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

struct PythonPathOutput(PathBuf);

impl PythonPathOutput {
    fn create() -> Result<Self, String> {
        for _ in 0..100 {
            let sequence = PYTHON_PATH_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let path = std::env::temp_dir().join(format!(
                "mcp-console-managed-python-{}-{sequence}",
                process::id()
            ));
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(_) => return Ok(Self(path)),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
                Err(error) => {
                    return Err(format!(
                        "failed to create managed Python resolver output `{}`: {error}",
                        path.display()
                    ));
                }
            }
        }
        Err("failed to allocate managed Python resolver output path".to_string())
    }

    fn path(&self) -> &Path {
        &self.0
    }

    fn python(&self) -> Result<PathBuf, String> {
        let output = fs::read(&self.0).map_err(|error| {
            format!(
                "failed to read managed Python resolver output `{}`: {error}",
                self.0.display()
            )
        })?;
        let output = String::from_utf8(output)
            .map_err(|_| "managed Python resolver returned a non-UTF-8 path".to_string())?;
        let python = PathBuf::from(output.trim());
        if !python.is_absolute() || !python.is_file() {
            return Err(format!(
                "managed Python resolver returned invalid interpreter `{}`",
                python.display()
            ));
        }
        Ok(python)
    }
}

impl Drop for PythonPathOutput {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

impl ManagedPython {
    pub(crate) fn configure_worker(&self, command: &mut crate::sandbox::SandboxedCommand) {
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

pub(crate) fn resolve_python(
    requirements: &[String],
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    let requirements = manifest_from_packages(requirements);
    resolve_python_host(requirements, configuration, managed_r, on_started)
}

pub(crate) fn resolve_python_manifest(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    crate::python_requirement::validate_all(&requirements.packages)?;
    crate::python_requirement::validate_version_constraints(&requirements.python_version)?;
    let requirements = requirements.normalized();
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let versions =
        resolve_python_versions_with(configuration, managed_r, &resolver, &mut on_started)?;
    let resolved_python = versions
        .resolve(&requirements.python_version)
        .map_err(|error| format!("managed Python version resolution failed: {}", error.trim()))?;
    let output_path = PythonPathOutput::create()?;
    let output = run_managed_python_resolver(
        &requirements,
        &resolved_python,
        output_path.path(),
        configuration,
        managed_r,
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
            "managed Python resolution failed:\nresolver input:\n{input}\nuv output:\n{error}"
        ));
    }
    check_resolver_control(&resolver, "managed Python resolution")?;

    let python = output_path.python()?;
    warm_matplotlib(&python, &resolver, &mut on_started)?;
    Ok(ManagedPython {
        python,
        requirements,
    })
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: &super::ManagedR,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    crate::python_requirement::validate_version_constraints(&constraints)?;
    let versions = resolve_python_versions(configuration, Some(managed_r), on_started)?;
    versions
        .resolve(&constraints)
        .map_err(|error| format!("managed Python version resolution failed: {}", error.trim()))
}

pub(crate) fn resolve_python_host(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    resolve_python_manifest(requirements, configuration, managed_r, on_started)
}

fn resolve_python_versions<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: F,
) -> Result<super::python_version::PythonVersions, String>
where
    F: FnOnce(ResolverStopHandle) -> Result<(), String>,
{
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    resolve_python_versions_with(configuration, managed_r, &resolver, &mut on_started)
}

fn resolve_python_versions_with<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
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
            let versions = run_uv_python_list(
                configuration,
                managed_r,
                resolver,
                on_started,
                managed,
                true,
            )?;
            if versions.is_empty() {
                return Ok(run_uv_python_list(
                    configuration,
                    managed_r,
                    resolver,
                    on_started,
                    system,
                    false,
                )?
                .rank(false));
            }
            Ok(versions.rank(true))
        }
        Some(preference) if preference == OsStr::new("managed") => {
            let mut versions = run_uv_python_list(
                configuration,
                managed_r,
                resolver,
                on_started,
                managed,
                true,
            )?;
            versions.extend(run_uv_python_list(
                configuration,
                managed_r,
                resolver,
                on_started,
                system,
                false,
            )?);
            Ok(versions.rank(true))
        }
        Some(preference) if preference == OsStr::new("system") => {
            let mut versions = run_uv_python_list(
                configuration,
                managed_r,
                resolver,
                on_started,
                managed,
                true,
            )?;
            versions.extend(run_uv_python_list(
                configuration,
                managed_r,
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
                managed_r,
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
    managed_r: Option<&super::ManagedR>,
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
    configure_python_resolver(&mut command, configuration, managed_r)?;
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
    managed_r: Option<&super::ManagedR>,
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
    configure_python_resolver(&mut command, configuration, managed_r)?;
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

fn configure_python_resolver(
    command: &mut Command,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
) -> Result<(), String> {
    if let Some(managed_r) = managed_r {
        managed_r.configure_resolver(command)?;
    }
    configuration.configure_direct(command)
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
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run {kind} resolver with `{}`: {error}",
            program.display()
        )
    })?;
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    if let Some(on_started) = on_started.take()
        && let Err(error) = on_started(resolver.stop_handle())
    {
        let _ = stop_resolver(&mut child, program, kind);
        return Err(error);
    }
    resolver.watch_exit(child.id());
    resolver.wait(&mut child, completed_write(), stdout, stderr, program, kind)
}

fn manifest_from_packages(
    requirements: &[String],
) -> crate::worker_protocol::PythonRequirementManifest {
    let mut manifest = crate::worker_protocol::default_python_requirement_manifest();
    manifest.packages.extend(requirements.iter().cloned());
    manifest.normalized()
}
