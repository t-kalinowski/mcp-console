use std::path::{Path, PathBuf};
use std::process::Stdio;

use serde::Serialize;

use super::process::{
    ResolverOutput, ResolverProcess, ResolverStopHandle, read_output, resolver_command,
    stop_resolver, write_input,
};

const PYTHON_RESOLVER: &str = r#"
base::local({
  input <- base::paste(base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  ), collapse = "\n")
  input <- jsonlite::fromJSON(input)
  packages <- base::unique(c("numpy", "pandas", input$packages))
  python_version <- input$python_version
  if (!base::length(python_version)) {
    python_version <- NULL
  }
  exclude_newer <- input$exclude_newer
  if (!base::length(exclude_newer)) {
    exclude_newer <- NULL
  }
  messages <- utils::capture.output(
    ignored_output <- utils::capture.output(
      python <- base::try(
        reticulate:::uv_get_or_create_env(
          packages = packages,
          python_version = python_version,
          exclude_newer = exclude_newer
        ),
        silent = TRUE
      ),
      type = "output"
    ),
    type = "message"
  )
  if (base::inherits(python, "try-error")) {
    python_selection <- base::grep(
      "^[[:space:]]*Python:",
      messages,
      value = TRUE
    )
    uv_error <- base::any(base::startsWith(messages, "uv error code: "))
    if (uv_error && base::length(python_selection)) {
      base::cat(
        base::sub(
          "^[[:space:]]*Python:[[:space:]]*",
          "",
          python_selection[[1L]]
        ),
        "\n",
        sep = ""
      )
    } else {
      error <- base::attr(python, "condition")
      base::writeLines(base::conditionMessage(error), con = base::stderr())
    }
    base::quit(save = "no", status = 1L, runLast = FALSE)
  }
  base::stopifnot(
    base::length(python) == 1L,
    !base::is.na(python),
    base::nzchar(python)
  )
  ignored_status <- base::system2(
    python,
    c("-I", "-c", base::shQuote("import matplotlib.font_manager")),
    stdout = FALSE,
    stderr = FALSE
  )
  base::cat(python, "\n", sep = "")
})
"#;

const PYTHON_VERSION_RESOLVER: &str = r#"
base::local({
  input <- base::paste(base::readLines(
    base::file("stdin", encoding = "UTF-8"),
    warn = FALSE
  ), collapse = "\n")
  constraints <- base::unlist(
    jsonlite::fromJSON(input),
    use.names = FALSE
  )
  if (!base::length(constraints)) {
    constraints <- NULL
  }
  version <- base::try(
    reticulate:::resolve_python_version(constraints),
    silent = TRUE
  )
  if (base::inherits(version, "try-error")) {
    error <- base::attr(version, "condition")
    base::writeLines(base::conditionMessage(error), con = base::stderr())
    base::quit(save = "no", status = 1L, runLast = FALSE)
  }
  base::stopifnot(
    base::length(version) == 1L,
    !base::is.na(version),
    base::nzchar(version)
  )
  base::cat(version, "\n", sep = "")
})
"#;

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
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    let requirements = manifest_from_packages(requirements);
    resolve_python_host(requirements, configuration, on_started)
}

pub(crate) fn resolve_python_manifest(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    crate::python_requirement::validate_all(&requirements.packages)?;
    let requirements = requirements.normalized();
    let input = serde_json::to_vec(&requirements)
        .expect("managed Python requirements should serialize as JSON");
    let output = run_python_resolver(
        PYTHON_RESOLVER,
        input,
        configuration,
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
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<String, String> {
    let input = serde_json::to_vec(&constraints)
        .expect("managed Python version constraints should serialize as JSON");
    let output = run_python_resolver(
        PYTHON_VERSION_RESOLVER,
        input,
        configuration,
        on_started,
        "managed Python version",
    )?;
    if !output.status.success() {
        let error = String::from_utf8_lossy(&output.stderr);
        return Err(format!(
            "managed Python version resolution failed with {}: {}",
            output.status,
            error.trim()
        ));
    }
    output
        .write_result
        .map_err(|error| format!("failed to write Python version constraints: {error}"))?;
    let version = String::from_utf8(output.stdout)
        .map_err(|_| "managed Python version resolver returned non-UTF-8 output".to_string())?;
    let version = version.trim();
    if version.is_empty() || version.lines().count() != 1 {
        return Err("managed Python version resolver returned an invalid version".to_string());
    }
    Ok(version.to_string())
}

pub(crate) fn resolve_python_host(
    requirements: crate::worker_protocol::PythonRequirementManifest,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
) -> Result<ManagedPython, String> {
    resolve_python_manifest(requirements, configuration, on_started)
}

fn run_python_resolver(
    source: &str,
    input: Vec<u8>,
    configuration: &super::ManagedPythonResolverConfiguration,
    on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    kind: &str,
) -> Result<ResolverOutput, String> {
    let rscript = configuration.rscript()?;
    let mut command = resolver_command(rscript);
    command
        .args(["--vanilla", "-e", source])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configuration.configure(&mut command)?;
    // Managed resolution intentionally runs outside the sandbox because
    // reticulate and uv need normal host network and cache access. Resolver
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
