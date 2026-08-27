use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, completed_write, read_output,
    resolver_command, stop_resolver,
};

const MANAGED_R_LIBRARY_RESOLVER_SOURCE: &str = include_str!("programs/r_library.R");
const MINIMUM_IR_VERSION: semver::Version = semver::Version::new(0, 4, 0);

struct IrCommand {
    program: &'static str,
    arguments: &'static [&'static str],
}

impl IrCommand {
    fn command(&self) -> Command {
        let mut command = resolver_command(Path::new(self.program));
        command.args(self.arguments);
        command
    }

    fn label(&self) -> String {
        std::iter::once(self.program)
            .chain(self.arguments.iter().copied())
            .collect::<Vec<_>>()
            .join(" ")
    }
}

#[derive(Clone)]
pub(crate) struct ManagedR {
    library: PathBuf,
    r_libs: std::ffi::OsString,
    rscript: PathBuf,
    requirements: Vec<String>,
}

impl ManagedR {
    pub(crate) fn configure_worker(
        &self,
        command: &mut crate::sandbox::SandboxedCommand,
    ) -> Result<(), String> {
        if !self.library.is_dir() {
            return Err(format!(
                "resolved R library `{}` no longer exists",
                self.library.display()
            ));
        }
        command.env("R_LIBS", &self.r_libs);
        Ok(())
    }

    pub(crate) fn requirements(&self) -> &[String] {
        &self.requirements
    }

    pub(crate) fn library(&self) -> &Path {
        &self.library
    }

    pub(crate) fn configure_resolver(&self, command: &mut Command) -> Result<(), String> {
        if !self.library.is_dir() {
            return Err(format!(
                "resolved R library `{}` no longer exists",
                self.library.display()
            ));
        }
        command.env("R_LIBS", &self.r_libs);
        Ok(())
    }

    pub(crate) fn rscript(&self) -> &Path {
        &self.rscript
    }
}

pub(crate) fn resolve_r(
    requirements: Vec<String>,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedR, String> {
    let resolver = ResolverProcess::new();
    let mut on_started = Some(on_started);
    let rscript = match std::env::var("R_HOME") {
        Ok(r_home) => PathBuf::from(r_home).join("bin/Rscript"),
        Err(_) => {
            let program = Path::new("R");
            let mut command = resolver_command(program);
            command
                .arg("RHOME")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let mut child = command.spawn().map_err(|error| {
                format!(
                    "failed to discover the worker R home with `{}`: {error}",
                    program.display()
                )
            })?;
            let output = collect_resolver_output(
                &resolver,
                &mut child,
                &mut on_started,
                program,
                "worker R home",
            )?;
            if !output.status.success() {
                let stderr = String::from_utf8_lossy(&output.stderr);
                return Err(format!(
                    "worker R home discovery failed with {}: {}",
                    output.status,
                    stderr.trim()
                ));
            }
            let r_home = String::from_utf8(output.stdout)
                .map_err(|error| format!("worker R returned a non-UTF-8 home path: {error}"))?;
            let r_home = r_home.trim();
            if r_home.is_empty() {
                return Err("worker R returned an empty home path".to_string());
            }
            PathBuf::from(r_home).join("bin/Rscript")
        }
    };
    let ir = ir_command(&resolver, &mut on_started)?;
    let mut command = ir.command();
    command.arg("run").arg("--rscript").arg(&rscript);
    for requirement in &requirements {
        command.arg("--with").arg(requirement);
    }
    command
        .env("IR_NO_LOCAL_SOURCES", "1")
        .args([
            "--isolated",
            "--vanilla",
            "-e",
            MANAGED_R_LIBRARY_RESOLVER_SOURCE,
        ])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // IR resolves and installs remote packages with normal host cache and
    // network access. Requirement strings are process arguments, never R source.
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run R package resolver with `{}`: {error}",
            ir.label()
        )
    })?;
    let output = collect_resolver_output(
        &resolver,
        &mut child,
        &mut on_started,
        Path::new(ir.program),
        "R package",
    )?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(format!(
            "R package resolution failed with {}: {detail}",
            output.status
        ));
    }

    let output = String::from_utf8(output.stdout)
        .map_err(|_| "R package resolver returned a non-UTF-8 path".to_string())?;
    let library = PathBuf::from(output);
    if !library.is_absolute() || !library.is_dir() {
        return Err(format!(
            "R package resolver returned invalid library `{}`",
            library.display()
        ));
    }
    let mut libraries = vec![library.clone()];
    if let Some(inherited) = std::env::var_os("R_LIBS") {
        libraries
            .extend(std::env::split_paths(&inherited).filter(|path| !path.as_os_str().is_empty()));
    }
    let r_libs = std::env::join_paths(libraries)
        .map_err(|error| format!("failed to construct R library path: {error}"))?;
    Ok(ManagedR {
        library,
        r_libs,
        rscript,
        requirements,
    })
}

fn ir_command(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
) -> Result<IrCommand, String> {
    let ir = if path_contains_entry("ir") {
        IrCommand {
            program: "ir",
            arguments: &[],
        }
    } else if path_contains_entry("uvx") {
        IrCommand {
            program: "uvx",
            arguments: &["--from", "r-lib-ir", "ir"],
        }
    } else {
        return Err("R package resolution requires `ir` or host `uvx` on PATH".to_string());
    };
    validate_ir_version(resolver, on_started, &ir)?;
    Ok(ir)
}

fn path_contains_entry(program: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    // A broken symlink or non-executable entry is a broken installation, not
    // permission to select a different resolver.
    std::env::split_paths(&path)
        .any(|directory| std::fs::symlink_metadata(directory.join(program)).is_ok())
}

fn validate_ir_version(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    ir: &IrCommand,
) -> Result<(), String> {
    let mut command = ir.command();
    command
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to check R package resolver version with `{}`: {error}",
            ir.label()
        )
    })?;
    let output = collect_resolver_output(
        resolver,
        &mut child,
        on_started,
        Path::new(ir.program),
        "R package",
    )?;
    if !output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let stderr = String::from_utf8_lossy(&output.stderr);
        let detail = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(format!(
            "failed to check R package resolver version with {}: {detail}",
            output.status
        ));
    }

    let output = String::from_utf8(output.stdout)
        .map_err(|_| "R package resolver returned a non-UTF-8 version".to_string())?;
    let reported = output.trim();
    let version = reported
        .strip_prefix("ir ")
        .and_then(|version| semver::Version::parse(version).ok())
        .ok_or_else(|| {
            format!(
                "R package resolution requires ir {MINIMUM_IR_VERSION} or later; could not parse `{reported}`"
            )
        })?;
    if version < MINIMUM_IR_VERSION {
        return Err(format!(
            "R package resolution requires ir {MINIMUM_IR_VERSION} or later; found ir {version}"
        ));
    }
    Ok(())
}

fn collect_resolver_output(
    resolver: &ResolverProcess,
    child: &mut std::process::Child,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    program: &Path,
    kind: &str,
) -> Result<ResolverOutput, String> {
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    if let Some(on_started) = on_started.take()
        && let Err(error) = on_started(resolver.stop_handle())
    {
        let _ = stop_resolver(child, program, kind);
        return Err(error);
    }
    resolver.watch_exit(child.id());
    resolver.wait(child, completed_write(), stdout, stderr, program, kind)
}
