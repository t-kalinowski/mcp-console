use std::os::unix::process::CommandExt as _;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use super::process::{
    ResolverProcess, ResolverStopHandle, completed_write, read_output, stop_resolver,
};

const R_LIBRARY_RESOLVER: &str = r#"
base::cat(base::normalizePath(
  base::.libPaths()[[1L]],
  winslash = "/",
  mustWork = TRUE
))
"#;

#[derive(Clone)]
pub(crate) struct ManagedR {
    library: PathBuf,
    r_libs: std::ffi::OsString,
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
            let mut command = Command::new(program);
            command
                .arg("RHOME")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped())
                .process_group(0);
            let mut child = command.spawn().map_err(|error| {
                format!(
                    "failed to discover the worker R home with `{}`: {error}",
                    program.display()
                )
            })?;
            let stdout = read_output(child.stdout.take().expect("R stdout is piped"));
            let stderr = read_output(child.stderr.take().expect("R stderr is piped"));
            let stop_handle = resolver.stop_handle();
            if let Err(error) = on_started
                .take()
                .expect("resolver start callback should be available")(
                stop_handle
            ) {
                let _ = stop_resolver(&mut child, program, "worker R home");
                return Err(error);
            }
            resolver.watch_exit(child.id());
            let output = resolver.wait(
                &mut child,
                completed_write(),
                stdout,
                stderr,
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
    let mut command = Command::new(program);
    command.arg("run").arg("--rscript").arg(&rscript);
    for requirement in &requirements {
        command.arg("--with").arg(requirement);
    }
    command
        .args(["--isolated", "--vanilla", "-e", R_LIBRARY_RESOLVER])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    // IR resolves and installs packages with normal host cache and network
    // access. Requirement strings are process arguments, never R source.
    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to run R package resolver with `{}`: {error}",
            program.display()
        )
    })?;
    let stdout = read_output(child.stdout.take().expect("resolver stdout is piped"));
    let stderr = read_output(child.stderr.take().expect("resolver stderr is piped"));
    if let Some(on_started) = on_started.take() {
        let stop_handle = resolver.stop_handle();
        if let Err(error) = on_started(stop_handle) {
            let _ = stop_resolver(&mut child, program, "R package");
            return Err(error);
        }
    }
    resolver.watch_exit(child.id());
    let output = resolver.wait(
        &mut child,
        completed_write(),
        stdout,
        stderr,
        program,
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
        requirements,
    })
}
