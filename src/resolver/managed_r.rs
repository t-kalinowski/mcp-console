use std::path::{Path, PathBuf};
use std::process::Stdio;

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, completed_write, read_output,
    resolver_command, stop_resolver,
};

const R_LIBRARY_RESOLVER: &str = r#"
base::cat(base::normalizePath(
  base::.libPaths()[[1L]],
  winslash = "/",
  mustWork = TRUE
))
"#;
const MINIMUM_IR_VERSION: semver::Version = semver::Version::new(0, 4, 0);

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
    let program = Path::new("ir");
    validate_ir_version(&resolver, &mut on_started, program)?;
    let mut command = resolver_command(program);
    command.arg("run").arg("--rscript").arg(&rscript);
    for requirement in &requirements {
        command.arg("--with").arg(requirement);
    }
    command
        .env("IR_NO_LOCAL_SOURCES", "1")
        .args(["--isolated", "--vanilla", "-e", R_LIBRARY_RESOLVER])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // IR resolves and installs remote packages with normal host cache and
    // network access. Requirement strings are process arguments, never R source.
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run R package resolver with `{}`: {error}",
            program.display()
        )
    })?;
    let output =
        collect_resolver_output(&resolver, &mut child, &mut on_started, program, "R package")?;
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

fn validate_ir_version(
    resolver: &ResolverProcess,
    on_started: &mut Option<impl FnOnce(ResolverStopHandle) -> Result<(), String>>,
    program: &Path,
) -> Result<(), String> {
    let mut command = resolver_command(program);
    command
        .arg("--version")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to check R package resolver version with `{}`: {error}",
            program.display()
        )
    })?;
    let output = collect_resolver_output(resolver, &mut child, on_started, program, "R package")?;
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
