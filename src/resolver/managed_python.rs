use std::ffi::OsStr;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use serde::Serialize;

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, completed_write, read_output,
    resolver_command, stop_resolver, write_input,
};

const MANAGED_PYTHON_ENVIRONMENT_RESOLVER_SOURCE: &str = include_str!("programs/managed_python.R");

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
    let input = serde_json::to_vec(&requirements)
        .expect("managed Python requirements should serialize as JSON");
    let output = run_python_resolver(
        MANAGED_PYTHON_ENVIRONMENT_RESOLVER_SOURCE,
        input,
        configuration,
        managed_r,
        on_started,
        "managed Python",
    )?;
    if !output.status.success() {
        let python = String::from_utf8_lossy(&output.stdout);
        let error = String::from_utf8_lossy(&output.stderr);
        let python = python.trim();
        let error = error.trim();
        return if python.is_empty() {
            Err(format!(
                "managed Python resolution failed with {}: {error}",
                output.status
            ))
        } else {
            let packages = requirements.packages.iter().map(String::as_str).collect();
            let python_version = requirements
                .python_version
                .iter()
                .map(String::as_str)
                .collect();
            let input = serde_json::to_string_pretty(&ResolverInput {
                python,
                packages,
                python_version,
                exclude_newer: requirements.exclude_newer.as_deref(),
            })
            .expect("resolver input strings should serialize as JSON");
            Err(format!(
                "managed Python resolution failed:\nresolver input:\n{input}\nuv output:\n{error}"
            ))
        };
    }
    output
        .write_result
        .map_err(|error| format!("failed to write Python requirements: {error}"))?;

    let output = String::from_utf8(output.stdout)
        .map_err(|_| "managed Python resolver returned a non-UTF-8 path".to_string())?;
    let python = PathBuf::from(output.trim());
    if !python.is_absolute() || !python.is_file() {
        return Err(format!(
            "managed Python resolver returned invalid interpreter `{}`",
            python.display()
        ));
    }
    Ok(ManagedPython {
        python,
        requirements,
    })
}

pub(crate) fn resolve_python_version(
    constraints: Vec<String>,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    crate::python_requirement::validate_version_constraints(&constraints)?;
    let versions = resolve_python_versions(configuration, managed_r, on_started)?;
    versions.resolve(&constraints).map_err(|error| {
        format!(
            "managed Python version resolution failed with exit status: 1: {}",
            error.trim()
        )
    })
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
    let configured_preference = configuration.python_preference();
    let preference = configured_preference.unwrap_or_else(|| OsStr::new("only-managed"));
    let versions = run_uv_python_list(
        configuration,
        managed_r,
        &resolver,
        &mut on_started,
        preference,
    )?;
    if versions.raw_empty() && configured_preference.is_none() {
        return run_uv_python_list(
            configuration,
            managed_r,
            &resolver,
            &mut on_started,
            OsStr::new("only-system"),
        );
    }
    Ok(versions)
}

fn run_uv_python_list<F>(
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    resolver: &ResolverProcess,
    on_started: &mut Option<F>,
    preference: &OsStr,
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
    configuration.configure_direct(managed_r, &mut command)?;
    let output = run_resolver_command(
        command,
        resolver,
        on_started,
        program,
        "managed Python version",
    )?;
    if !output.status.success() {
        return Err(python_list_failure(
            program,
            preference,
            &output,
            managed_r.is_some(),
        ));
    }
    output.write_result.map_err(|error| {
        format!("failed to prepare managed Python version resolver input: {error}")
    })?;
    let prefer_managed =
        preference != OsStr::new("system") && preference != OsStr::new("only-system");
    super::python_version::PythonVersions::parse(&output.stdout, prefer_managed).map_err(|error| {
        format!("managed Python version resolver returned invalid output: {error}")
    })
}

fn python_list_failure(
    program: &Path,
    preference: &OsStr,
    output: &ResolverOutput,
    preserve_reticulate_diagnostic: bool,
) -> String {
    let error = String::from_utf8_lossy(&output.stderr);
    let status = output
        .status
        .code()
        .map_or_else(|| output.status.to_string(), |status| status.to_string());
    let mut message = format!(
        "managed Python version resolution failed with exit status: 1: {}",
        error.trim()
    );
    if !preserve_reticulate_diagnostic {
        return format!(
            "managed Python version resolution failed with {}: {}",
            output.status,
            error.trim()
        );
    }
    // Keep the existing R/jsonlite failure transcript stable in full mode while
    // process ownership moves from the R helper to this direct `uv` invocation.
    if output.stdout.is_empty() {
        message.push_str("\nparse error: premature EOF\n\n                             ");
        message.push_str("(right here) ------^\n\n");
    }
    message.push_str(&format!(
        "Warning message:\nIn system2(uv, args, ...) :\n  running command ''{}' python list --all-versions --color never --output-format json --python-preference  {}' had status {status}",
        program.display(),
        preference.to_string_lossy()
    ));
    message
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

fn run_python_resolver(
    source: &str,
    input: Vec<u8>,
    configuration: &super::ManagedPythonResolverConfiguration,
    managed_r: Option<&super::ManagedR>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    kind: &str,
) -> Result<ResolverOutput, String> {
    let managed_r = managed_r
        .ok_or_else(|| "managed Python resolver requires a managed R environment".to_string())?;
    let rscript = managed_r.rscript();
    let mut command = resolver_command(rscript);
    command
        .args(["--vanilla", "-e", source])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configuration.configure(managed_r, &mut command)?;
    // Managed resolution intentionally runs outside the sandbox because
    // reticulate and `uv` need normal host network and cache access. Resolver
    // inputs are JSON standard-input data, never R source.
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run {kind} resolver with `{}`: {error}",
            rscript.display()
        )
    })?;
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    let stdin = child.stdin.take().expect("resolver stdin is piped");
    let resolver = ResolverProcess::new();
    let stop_handle = resolver.stop_handle();
    if let Err(error) = on_started(stop_handle) {
        let _ = stop_resolver(&mut child, rscript, kind);
        return Err(error);
    }
    resolver.watch_exit(child.id());
    let input = write_input(stdin, input);
    resolver.wait(&mut child, input, stdout, stderr, rscript, kind)
}

fn manifest_from_packages(
    requirements: &[String],
) -> crate::worker_protocol::PythonRequirementManifest {
    let mut manifest = crate::worker_protocol::default_python_requirement_manifest();
    manifest.packages.extend(requirements.iter().cloned());
    manifest.normalized()
}
