//! Resolve a client launch command without retaining disposable cache paths.

use std::fs;
use std::io::{ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use crate::cli::ConfigureClientOptions;

pub(super) struct Launch {
    pub(super) command: String,
    pub(super) args: Vec<String>,
    install: Option<String>,
}

impl Launch {
    pub(super) fn resolve(
        options: &ConfigureClientOptions,
        directory: &Path,
    ) -> Result<Self, String> {
        let mut launch = Self {
            command: "uvx".into(),
            args: vec![match &options.pin_version {
                Some(version) => format!("mcp-console@{version}"),
                None => "mcp-console".into(),
            }],
            install: None,
        };
        if options.no_uvx {
            let executable = std::env::current_exe()
                .and_then(|path| path.canonicalize())
                .map_err(|error| format!("Cannot locate this executable: {error}"))?;
            let target = if in_cache(&executable)? {
                launch.install = Some(installed_requirement(&executable)?);
                // uv marks both its normal cache and --no-cache temporary roots.
                // Query its persistent launcher directory before any installation
                // so dry-run can show the same command and configuration path.
                let output = Command::new("uv")
                    .args(["tool", "dir", "--bin"])
                    .stdin(Stdio::null())
                    .output()
                    .map_err(|error| format!("Cannot run uv tool dir --bin: {error}"))?;
                if !output.status.success() {
                    return Err(format!(
                        "uv tool dir --bin failed: {}",
                        String::from_utf8_lossy(&output.stderr).trim()
                    ));
                }
                let bin = String::from_utf8(output.stdout).map_err(|error| error.to_string())?;
                let bin = bin.trim();
                if bin.is_empty() {
                    return Err("uv tool dir --bin returned no directory".into());
                }
                directory
                    .join(bin)
                    .join(format!("mcp-console{}", std::env::consts::EXE_SUFFIX))
            } else {
                executable
            };
            launch.command = target
                .into_os_string()
                .into_string()
                .map_err(|_| "Executable path is not valid UTF-8")?;
            launch.args.clear();
        }
        launch.args.push("serve".into());
        launch.args.extend(options.serve_args.iter().cloned());
        Ok(launch)
    }

    pub(super) fn prepare(&self, dry_run: bool) -> Result<(), String> {
        if let Some(package) = &self.install {
            let action = if dry_run {
                "Would install"
            } else {
                "Installing"
            };
            println!("{action} a persistent executable: uv tool install {package:?}");
            println!("Executable: {}", self.command);
            if !dry_run {
                std::io::stdout()
                    .flush()
                    .map_err(|error| error.to_string())?;
                let status = Command::new("uv")
                    .args(["tool", "install", package])
                    .stdin(Stdio::null())
                    .status()
                    .map_err(|error| format!("Cannot run uv tool install: {error}"))?;
                if !status.success() {
                    return Err(format!(
                        "uv tool install failed with {status}; no client configuration files changed"
                    ));
                }
                if !PathBuf::from(&self.command).is_file() {
                    return Err(format!("uv tool install did not create {}", self.command));
                }
            }
        }
        Ok(())
    }
}

fn installed_requirement(executable: &Path) -> Result<String, String> {
    let environment = executable
        .ancestors()
        .find(|directory| directory.join("pyvenv.cfg").is_file())
        .ok_or("Cannot locate this temporary tool's environment; install Console persistently with uv tool install <SOURCE> before using --no-uvx")?;
    // Let uv reconstruct its installed requirement, including direct URLs,
    // archive hashes, and resolved Git commits. A version alone loses the
    // source selected by uvx --from. Never guess when metadata is unavailable.
    let output = Command::new("uv")
        .args(["pip", "freeze", "--python"])
        .arg(environment)
        .args(["--color", "never"])
        .stdin(Stdio::null())
        .output()
        .map_err(|error| {
            format!("Cannot read this temporary tool's source with uv pip freeze: {error}")
        })?;
    if !output.status.success() {
        return Err(format!(
            "uv pip freeze failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let requirements = String::from_utf8(output.stdout).map_err(|error| error.to_string())?;
    requirements
        .lines()
        .find_map(|requirement| {
            if let Some(version) = requirement.strip_prefix("mcp-console==") {
                Some(format!("mcp-console@{version}"))
            } else if requirement.starts_with("mcp-console @ ") {
                Some(requirement.to_owned())
            } else {
                None
            }
        })
        .ok_or_else(|| "Cannot identify this temporary tool's source; install Console persistently with uv tool install <SOURCE> before using --no-uvx".into())
}

fn in_cache(executable: &Path) -> Result<bool, String> {
    // uv tags every virtualenv, including persistent tools and project venvs.
    // Only a containing cache makes the executable disposable. Its marker also
    // handles custom cache directories and --no-cache temporary cache roots.
    for directory in executable.parent().unwrap().ancestors() {
        if directory.join("pyvenv.cfg").is_file() {
            continue;
        }
        let marker = directory.join("CACHEDIR.TAG");
        match fs::read(&marker) {
            Ok(contents)
                if contents.starts_with(b"Signature: 8a477f597d28d172789f06886806bc55") =>
            {
                return Ok(true);
            }
            Ok(_) => {}
            Err(error) if error.kind() == ErrorKind::NotFound => {}
            Err(error) => return Err(format!("Cannot read {}: {error}", marker.display())),
        }
    }
    Ok(false)
}
