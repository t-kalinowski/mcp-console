use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use rmcp::{
    RoleServer, ServerHandler, ServiceExt,
    handler::server::{
        common::Extension, router::tool::ToolRouter, tool::ToolCallContext, wrapper::Parameters,
    },
    model::{CallToolRequestParams, CallToolResult, ContentBlock, ErrorData},
    schemars,
    service::RequestContext,
    tool, tool_handler, tool_router,
};
use serde::Deserialize;
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

use crate::worker_client::WORKER_SHUTDOWN_GRACE;

const DEFAULT_TIMEOUT_MS: u64 = 60_000;
// Internal eval configuration; intentionally not exposed through the CLI.
const LANGUAGES_ENV: &str = "MCP_CONSOLE_LANGUAGES";

#[derive(Clone, Copy, Default)]
struct Languages {
    r: bool,
    python: bool,
    sql: bool,
}

impl Languages {
    fn from_environment() -> Result<Self, String> {
        let Some(value) = std::env::var_os(LANGUAGES_ENV) else {
            return Ok(Self::all());
        };
        let value = value
            .into_string()
            .map_err(|_| Self::invalid_configuration())?;
        let mut languages = Self::default();
        for language in value.split(',') {
            match language {
                "r" => languages.r = true,
                "python" => languages.python = true,
                "sql" => languages.sql = true,
                _ => return Err(Self::invalid_configuration()),
            }
        }
        Ok(languages)
    }

    fn all() -> Self {
        Self {
            r: true,
            python: true,
            sql: true,
        }
    }

    fn enables(self, language: crate::cell::Language) -> bool {
        match language {
            crate::cell::Language::R => self.r,
            crate::cell::Language::Python => self.python,
            crate::cell::Language::Sql => self.sql,
        }
    }

    fn field(language: crate::cell::Language) -> &'static str {
        match language {
            crate::cell::Language::R => "r",
            crate::cell::Language::Python => "python",
            crate::cell::Language::Sql => "sql",
        }
    }

    fn invalid_configuration() -> String {
        format!("`{LANGUAGES_ENV}` must be a comma-separated subset of `r`, `python`, and `sql`")
    }
}

#[derive(Clone)]
struct ConsoleServer {
    worker: crate::worker_client::Client,
    transcript: crate::transcript::Transcript,
    deliveries: crate::server_transport::ResponseDeliveries,
    languages: Languages,
    tool_router: ToolRouter<Self>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SendArguments {
    /// One complete R cell evaluated in persistent global state. Its final visible expression
    /// autoprints through R's normal console display; R also autoprints earlier visible top-level
    /// expressions. Leave the primary result last and print only when additional output is needed.
    /// The built-in worker resolves missing plain CRAN package names on demand through `library()`,
    /// `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`. Treat CRAN packages as
    /// available and use them directly; do not probe package availability or call
    /// `install.packages()`. Resolution makes a package available but attaches it only through the
    /// original `library()` or `require()` call. R source is not scanned in advance. Read Python
    /// globals through `py$name`. R data frames are directly queryable by name from later SQL cells.
    /// Access DuckDB tables and views through the borrowed `sql_connection()` with DBI or dplyr; do
    /// not disconnect it. Default-device plots return as PNG images. Keep all drawing operations for
    /// one plot in the same cell. Set persistent dimensions with
    /// `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`;
    /// width and height are in inches. Omit this field for polling or stdin-only calls.
    r: Option<String>,
    /// One complete Python cell evaluated in persistent `__main__` state. Its final visible expression
    /// autoprints through Python's normal display hook. Leave the primary result last and print only
    /// when additional output is needed. When an import is missing, the built-in managed worker
    /// resolves a PyPI distribution on demand, using a curated mapping for well-known
    /// import/distribution differences and otherwise assuming the distribution matches the top-level
    /// module. Python source is not scanned; resolution starts only when execution reaches the import.
    /// Use `requirements.python` when the distribution differs from the inferred name, exact registry
    /// metadata is needed, or the package should be prepared before the cell. A user-selected Python
    /// environment disables both automatic resolution and managed requirements; import packages
    /// already installed there directly. Read R globals and call R functions through `r.name`. Python
    /// data frames are not automatically visible to SQL; bind them to an R name first. At cell end,
    /// including after a Python error, every open `matplotlib.pyplot` figure returns once as a PNG image
    /// and is closed. `show()` is optional. R plots called through `r` follow the R plot rules. Omit this
    /// field for polling or stdin-only calls.
    python: Option<String>,
    /// One complete DuckDB SQL cell evaluated in the persistent catalog. The final query result returns
    /// a bounded preview. An unqualified relation name can query a data frame in R global state; a
    /// DuckDB table or view with the same name takes precedence. When attaching an existing DuckDB
    /// database outside the worker's private temporary directory, use
    /// `ATTACH 'path' AS name (READ_ONLY)`; the sandbox blocks DuckDB's default writable mode for those
    /// paths. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN` for discovery. DuckDB CLI dot
    /// commands are not supported. Omit this field for polling or stdin-only calls.
    sql: Option<String>,
    /// Applies lifecycle control alone or before compatible same-call fields. `interrupt` requests
    /// SIGINT from the active host resolver or live worker and preserves in-memory state. After
    /// successful delivery, stdin is queued and `send` waits a short grace before observing the
    /// earlier evaluation or attempting an optional following cell; the cell is not run if the
    /// interrupted evaluation remains active. `restart` resolves same-call requirements before
    /// replacement, discards R, Python, DuckDB, debugger, and unread-stdin state, then sends
    /// same-call stdin and code only to the replacement.
    control: Option<SendControl>,
    /// Additive R packages, Python packages, or DuckDB extensions to retain for later calls.
    /// Requirements alone perform standalone preparation. With one cell, they are preconditions of
    /// that cell. With `control = "restart"`, they are part of the restart transaction, with or
    /// without a cell. Requirements are not accepted with interrupt unless a cell follows.
    /// Preparation does not import, attach, or load dependencies. On a code-bearing call without
    /// control, preparation completes before same-call nonempty stdin is queued. Standalone
    /// preparation cannot queue nonempty stdin. With restart, failure leaves the current worker
    /// unchanged and sends neither stdin nor code. With interrupt and a following cell, signal
    /// delivery and stdin enqueue happen before requirements are validated or prepared and are not
    /// rolled back if that later work fails. Ordinary CRAN packages used by the built-in R worker need
    /// not be declared here; use `requirements.r` to stage packages ahead of evaluation or provide
    /// explicit IR references. In the built-in managed Python environment, missing imports normally
    /// resolve at runtime. Use `requirements.python` to stage a distribution before the cell, provide
    /// a version, extra, or marker, or correct automatic inference. Python source is not pre-scanned,
    /// and SQL does not trigger package discovery. A cell is not run if explicit preparation fails or
    /// further changes require restart. Resolution runs outside the worker sandbox and may download
    /// packages or extensions or execute installation or build code on the host. Use only trusted
    /// requirements.
    requirements: Option<Requirements>,
    /// Input for an active read, prompt, or debugger. When responding to active input, omit R, Python,
    /// and SQL code and send stdin on its own. Its UTF-8 encoding is queued exactly; no newline is added.
    /// Line-oriented input therefore normally needs a trailing `\n`. On a code-bearing call without
    /// control, requirements are prepared before nonempty stdin is queued. Standalone preparation
    /// cannot queue nonempty stdin. After `interrupt`, nonempty stdin is queued before the
    /// 100-millisecond grace and may be consumed while the earlier operation unwinds. After `restart`,
    /// same-call stdin is sent only to the replacement. When sent with a cell, nonempty text is queued
    /// before the code is run; an already waiting interactive read may consume it before the new cell
    /// begins. Empty text queues nothing. If output ends in `[waiting for stdin]`, send the requested
    /// input here. Unread text can satisfy later reads and is discarded by restart.
    stdin: Option<String>,
    /// Maximum time this call waits once a cell has been dispatched or the call has attached to an
    /// active evaluation, including one automatic worker replacement attempt. Reaching the timeout
    /// does not cancel evaluation, resolution, or startup. Inline control, interrupt grace, restart,
    /// and explicit requirement preparation happen before dispatch and may make the complete call take
    /// longer. This value does not limit standalone preparation. Automatic R and Python import
    /// resolution are part of the running evaluation and count toward this wait. On expiry, the call
    /// returns available output and a state marker, such as
    /// `[running; poll with an empty send]` or `[worker starting]`. If evaluation remains active, poll
    /// with an empty `send` call; do not resubmit the cell.
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

#[derive(Clone, Copy, Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(rename_all = "snake_case")]
enum SendControl {
    Interrupt,
    Restart,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(deny_unknown_fields)]
struct Requirements {
    /// Additive DuckDB extension names for standalone preparation, preparation before a cell, or a
    /// restart transaction, for example `fts`, `spatial`, or `excel`. JSON and ICU are already
    /// prepared for built-in workers. Names must start with a lowercase ASCII letter and contain
    /// only lowercase ASCII letters, digits, and underscores. The host resolver uses DuckDB's own
    /// `INSTALL` outside the sandbox, with DuckDB's default extension repository and native cache.
    /// Preparation does not load extension code; `LOAD` and automatic loading happen later inside
    /// the sandbox.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1, max = 64)))]
    duckdb: Vec<String>,
    /// Additive, single-line IR package references for standalone preparation, preparation before a
    /// cell, or a restart transaction, for example `data.table`, `sf`, or `yaml12`. Use this field
    /// to stage packages ahead of evaluation or supply an explicit supported remote IR reference.
    /// Automatic R discovery accepts only plain package names. An idle worker that implements R
    /// preparation can add requirements without losing live state. Local package sources are
    /// rejected because resolution runs with server permissions.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    r: Vec<String>,
    /// Additive, named PEP 508 registry requirements for standalone preparation, preparation before a
    /// cell, or a restart transaction, for example `polars>=1`, `scikit-learn`, or
    /// `matplotlib; python_version >= '3.10'`. Use explicit requirements when automatic import
    /// inference needs a different distribution, a version, an extra, or an environment marker, or
    /// when the distribution should be prepared before the cell. Automatic imports infer bare
    /// distribution names only. Paths, file URLs, editable requirements, direct references, local
    /// archives, and local projects are rejected. Preparation does not import the package. An idle
    /// server-managed worker may activate compatible additions without losing state. A nonempty
    /// user-selected `RETICULATE_PYTHON` disables automatic resolution and managed Python
    /// requirements.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    python: Vec<String>,
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>, relay: Option<PathBuf>) -> Result<Self, String> {
        let languages = Languages::from_environment()?;
        let tool_router = Self::configured_tool_router(languages);
        let transcript = crate::transcript::Transcript::new();
        let worker = match (worker, relay) {
            (Some(program), relay) => crate::worker_client::Client::new(program, relay)?,
            (None, None) => crate::worker_client::Client::builtin()?,
            (None, Some(_)) => return Err("a custom relay requires a custom worker".to_string()),
        };
        Ok(Self {
            worker,
            transcript,
            deliveries: crate::server_transport::ResponseDeliveries::default(),
            languages,
            tool_router,
        })
    }

    fn configured_tool_router(languages: Languages) -> ToolRouter<Self> {
        let mut router = Self::tool_router();
        let send = router
            .map
            .get_mut("send")
            .expect("send tool must be registered");
        let schema = Arc::make_mut(&mut send.attr.input_schema);
        let properties = schema
            .get_mut("properties")
            .and_then(serde_json::Value::as_object_mut)
            .expect("send schema must have object properties");
        let control = properties
            .get_mut("control")
            .and_then(serde_json::Value::as_object_mut)
            .expect("send control schema must be an object");
        control.insert(
            "type".to_string(),
            serde_json::Value::String("string".to_string()),
        );
        if let Some(values) = control
            .get_mut("enum")
            .and_then(serde_json::Value::as_array_mut)
        {
            values.retain(|value| !value.is_null());
        }
        // Keep the normal tool prose and nested requirements unchanged; evals
        // only need to project which direct code fields the client can call.
        for (field, enabled) in [
            ("r", languages.r),
            ("python", languages.python),
            ("sql", languages.sql),
        ] {
            if !enabled {
                properties.shift_remove(field);
            }
        }
        router
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(
        description = r#"Persistent R, Python, and DuckDB SQL workbench for exact computation, file and data inspection, transformation, visualization, statistics, simulation, and modeling. State persists across sequential calls. Reassess the language for each cell and switch whenever another language is a better fit; do not stay in one language solely because state already exists there. Use the available live bridges when switching: Python reads R globals through `r.name`, R reads Python globals through `py$name`, SQL can query R data frames by name, and R accesses DuckDB through `sql_connection()`.

Send one complete `r`, `python`, or `sql` cell per call. Code-bearing calls must be sequential because only one evaluation can be active. A control-only interrupt may overlap a pending `send` while that call resolves or prepares requirements, including for restart. When an intermediate result affects the next step, inspect it before sending another cell. R and Python display a final visible top-level expression, and SQL returns a bounded preview, so leave the primary result last and print only when additional output is needed. Cells are not transactional; changes made before an error may remain.

`send` is the sole interaction with the persistent console. Use one code field for evaluation. Omit code to poll, provide stdin, prepare requirements, interrupt, or restart. Requirements alone stage dependencies; requirements with a cell prepare its preconditions. `control = "restart"` can include requirements, stdin, and a cell. Restart discards in-memory R, Python, DuckDB, debugger, and unread-stdin state, then targets same-call stdin and code only at the replacement. `control = "interrupt"` can include stdin and optionally a following cell. Interrupt preserves in-memory state and waits 100 milliseconds before observing the earlier evaluation or attempting that cell; the cell is not run if the interrupted evaluation remains active. `timeout_ms = 0` gives the shortest post-grace observation after interrupt.

`timeout_ms` limits how long the call waits after dispatch or attachment. Inline control, interrupt grace, restart, and explicit requirement preparation can make the complete call take longer and do not consume the cell wait timeout. The timeout does not cancel startup, dependency resolution, or evaluation. If a response ends in `[running; poll with an empty send]`, call `send` again without code or stdin; do not resubmit the cell. Send `stdin` without code to answer an active prompt or debugger.

The built-in worker resolves ordinary CRAN packages and missing imports in its managed Python environment on demand. Use `requirements` for explicit R references, exact Python distribution metadata, or DuckDB extensions; preparation makes dependencies available but does not import, attach, or load them. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Evaluated code can read host files, cannot directly access the network, and can write only in the worker's private temporary directory. Dependency resolution runs outside the sandbox and may execute package installation or build code; use only trusted dependencies."#
    )]
    async fn send(
        &self,
        Extension(call): Extension<crate::transcript::Call>,
        Extension(delivery): Extension<crate::server_transport::ResponseDeliveryCall>,
        Parameters(SendArguments {
            r,
            python,
            sql,
            control,
            requirements,
            stdin,
            timeout_ms,
        }): Parameters<SendArguments>,
    ) -> Result<CallToolResult, String> {
        let cell = match (r, python, sql) {
            (Some(source), None, None) => Some(crate::cell::Cell {
                language: crate::cell::Language::R,
                source,
            }),
            (None, Some(source), None) => Some(crate::cell::Cell {
                language: crate::cell::Language::Python,
                source,
            }),
            (None, None, Some(source)) => Some(crate::cell::Cell {
                language: crate::cell::Language::Sql,
                source,
            }),
            (None, None, None) => None,
            _ => {
                return Err("only one of `r`, `python`, or `sql` may be supplied".to_string());
            }
        };
        if let Some(cell) = cell.as_ref()
            && !self.languages.enables(cell.language)
        {
            return Err(format!(
                "`{}` cells are disabled by `{LANGUAGES_ENV}`",
                Languages::field(cell.language)
            ));
        }
        let standalone_preparation = requirements.is_some() && cell.is_none() && control.is_none();
        if standalone_preparation && stdin.as_ref().is_some_and(|stdin| !stdin.is_empty()) {
            return Err(
                "requirements-only `send` performs standalone preparation and cannot also queue stdin"
                    .to_string(),
            );
        }
        if requirements.is_some()
            && cell.is_none()
            && matches!(control, Some(SendControl::Interrupt))
        {
            return Err(
                "`requirements` with `control = \"interrupt\"` requires a code cell".to_string(),
            );
        }
        let validation = requirements
            .as_ref()
            .map(validate_environment_requirements)
            .transpose();
        let deferred_validation_error = match validation {
            Err(error) if matches!(control, Some(SendControl::Interrupt)) => Some(error),
            Err(error) => return Err(error),
            Ok(_) => None,
        };
        let requirements = requirements.map(|Requirements { duckdb, r, python }| {
            let requirements = crate::worker_client::Requirements { duckdb, r, python };
            match deferred_validation_error {
                Some(error) => crate::worker_client::RequirementSubmission::Invalid(error),
                None => crate::worker_client::RequirementSubmission::Valid(requirements),
            }
        });
        if standalone_preparation {
            let Some(crate::worker_client::RequirementSubmission::Valid(requirements)) =
                requirements
            else {
                unreachable!("standalone requirements were validated before preparation")
            };
            let text = match self.worker.prepare(requirements).await? {
                crate::worker_client::PrepareResult::Prepared => "[prepared]",
                crate::worker_client::PrepareResult::RestartRequired => "[restart required]",
                crate::worker_client::PrepareResult::Failed(response)
                | crate::worker_client::PrepareResult::WorkerStopped(response) => {
                    return Ok(response_to_tool_result(
                        response,
                        &call,
                        &self.transcript,
                        &self.deliveries,
                        &delivery,
                    ));
                }
            };
            return Ok(CallToolResult::success(vec![ContentBlock::text(text)]));
        }
        let response = self
            .worker
            .send(crate::worker_client::SendRequest {
                cell,
                stdin,
                requirements,
                control: control.map(|control| match control {
                    SendControl::Interrupt => crate::worker_client::SendControl::Interrupt,
                    SendControl::Restart => crate::worker_client::SendControl::Restart,
                }),
                timeout: Duration::from_millis(timeout_ms),
                transcript: self.transcript.clone(),
                call_id: call.id(),
                test_operation: delivery.test_operation(),
            })
            .await?;
        Ok(response_to_tool_result(
            response,
            &call,
            &self.transcript,
            &self.deliveries,
            &delivery,
        ))
    }
}

fn response_to_tool_result(
    mut response: crate::worker_client::Response,
    call: &crate::transcript::Call,
    transcript: &crate::transcript::Transcript,
    deliveries: &crate::server_transport::ResponseDeliveries,
    delivery: &crate::server_transport::ResponseDeliveryCall,
) -> CallToolResult {
    if let Err(error) = response.persist_images(transcript, call.id()) {
        transcript.disable(error);
    }
    let (content, is_error, response_delivery) = response.into_parts();
    let mut result_images = Vec::new();
    let content = content
        .into_iter()
        .map(|content| match content {
            crate::worker_client::Content::Text(text) => ContentBlock::text(text),
            crate::worker_client::Content::Image {
                data,
                mime_type,
                artifact,
            } => {
                result_images.extend(artifact);
                ContentBlock::image(data, mime_type)
            }
        })
        .collect();
    if let Err(error) = call.record_result_images(result_images) {
        transcript.disable(error);
    }
    if let Some(response_delivery) = response_delivery {
        deliveries.register(delivery, response_delivery);
    }
    if is_error {
        CallToolResult::error(content)
    } else {
        CallToolResult::success(content)
    }
}

fn validate_r_requirements(r: &[String]) -> Result<(), String> {
    validate_requirements(r, "r", "R")
}

fn validate_python_requirements(python: &[String]) -> Result<(), String> {
    if python.len() > 64 {
        return Err("`requirements.python` accepts at most 64 requirements".to_string());
    }
    crate::python_requirement::validate_all(python)
}

fn validate_environment_requirements(requirements: &Requirements) -> Result<(), String> {
    if requirements.duckdb.is_empty() && requirements.r.is_empty() && requirements.python.is_empty()
    {
        return Err(
            "at least one of `requirements.r`, `requirements.python`, or `requirements.duckdb` is required"
                .to_string(),
        );
    }
    validate_duckdb_extensions(&requirements.duckdb)?;
    validate_r_requirements(&requirements.r)?;
    validate_python_requirements(&requirements.python)
}

fn validate_duckdb_extensions(extensions: &[String]) -> Result<(), String> {
    if extensions.len() > 64 {
        return Err("`requirements.duckdb` accepts at most 64 extensions".to_string());
    }
    if extensions.iter().any(|extension| extension.len() > 64) {
        return Err("DuckDB extension names must be at most 64 ASCII characters".to_string());
    }
    if extensions.iter().any(|extension| {
        let mut bytes = extension.bytes();
        !bytes.next().is_some_and(|byte| byte.is_ascii_lowercase())
            || bytes
                .any(|byte| !(byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_'))
    }) {
        return Err(
            "DuckDB extension names must start with a lowercase ASCII letter and contain only lowercase ASCII letters, digits, and underscores"
                .to_string(),
        );
    }
    Ok(())
}

fn validate_requirements(
    requirements: &[String],
    field: &str,
    language: &str,
) -> Result<(), String> {
    if requirements.len() > 64 {
        return Err(format!(
            "`requirements.{field}` accepts at most 64 requirements"
        ));
    }
    if requirements
        .iter()
        .any(|requirement| requirement.trim().is_empty())
    {
        return Err(format!("{language} requirement strings must not be empty"));
    }
    if requirements.iter().any(|requirement| {
        requirement
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
    }) {
        return Err(format!(
            "{language} requirement strings must not contain NUL or line breaks"
        ));
    }
    Ok(())
}

#[tool_handler(name = "mcp-console", router = self.tool_router)]
impl ServerHandler for ConsoleServer {
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        mut context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let request_id = context.id.clone();
        if request.name.as_ref() != "send" {
            return self
                .tool_router
                .call(ToolCallContext::new(self, request, context))
                .await;
        }
        // A response can be visible before its write future settles. Delay only
        // console operations so transport receipt remains live for cancellation and EOF.
        let admission = context
            .extensions
            .remove::<crate::server_transport::ResponseDeliveryAdmission>()
            .expect("console operations must carry transport admission");
        let (delivery, operation) = tokio::select! {
            biased;
            _ = context.ct.cancelled() => {
                return Err(ErrorData::internal_error(
                    "request cancelled before execution",
                    None,
                ));
            }
            delivery = admission.admit() => {
                match delivery {
                    Ok(delivery) => delivery,
                    Err(crate::server_transport::ResponseDeliveryAdmissionError::Cancelled) => {
                        return Err(ErrorData::internal_error(
                            "request cancelled before execution",
                            None,
                        ));
                    }
                    Err(crate::server_transport::ResponseDeliveryAdmissionError::Closed) => {
                        return Err(ErrorData::internal_error(
                            "MCP input closed before request execution",
                            None,
                        ));
                    }
                }
            }
        };
        context.extensions.insert(delivery);
        let transcript = self.transcript.clone();
        let request_meta = context.meta.clone();
        let request = Arc::new(request);
        let recording_request = Arc::clone(&request);
        let recorder = transcript.clone();
        let call = match tokio::task::spawn_blocking(move || {
            recorder.begin(&request_id, &request_meta, &recording_request)
        })
        .await
        {
            Ok(call) => call,
            Err(error) => {
                transcript.disable(format!("transcript task failed: {error}"));
                crate::transcript::Call::unrecorded()
            }
        };
        let request =
            Arc::into_inner(request).expect("transcript task should release the tool request");
        context.extensions.insert(call.clone());
        let result = Arc::new(
            self.tool_router
                .call(ToolCallContext::new(self, request, context))
                .await,
        );
        let recorder = transcript.clone();
        let recording_result = Arc::clone(&result);
        if let Err(error) = tokio::task::spawn_blocking(move || {
            recorder.finish(call, recording_result.as_ref());
        })
        .await
        {
            transcript.disable(format!("transcript task failed: {error}"));
        }
        let result =
            Arc::into_inner(result).expect("transcript task should release the tool result");
        operation.complete();
        result
    }
}

/// Runs the MCP stdio server and owns the selected worker.
///
/// Closing MCP input also stops a worker whose evaluation is still running.
pub async fn run(worker: Option<PathBuf>, relay: Option<PathBuf>) -> Result<(), Box<dyn Error>> {
    let server = ConsoleServer::new(worker, relay).map_err(std::io::Error::other)?;
    let worker = server.worker.clone();
    let (input_closed, wait_for_input_close) = oneshot::channel();
    let input = ShutdownReader::new(tokio::io::stdin(), input_closed);
    let transport = crate::server_transport::ServerTransport::new(
        input,
        tokio::io::stdout(),
        server.deliveries.clone(),
    );
    let service = server.serve(transport).await?;
    let shutdown = async move {
        let shutdown_started = wait_for_input_close
            .await
            .unwrap_or_else(|_| Instant::now());
        let deadline = shutdown_started + WORKER_SHUTDOWN_GRACE;
        worker.shutdown(deadline).await?;
        Ok::<(), String>(())
    };

    let (result, shutdown) = tokio::join!(service.waiting(), shutdown);
    shutdown.map_err(std::io::Error::other)?;
    result?;
    Ok(())
}

/// Reports EOF to the worker owner while otherwise behaving like its input.
/// Dropping the reader also wakes the owner by closing the one-shot channel.
struct ShutdownReader<R> {
    inner: R,
    input_closed: Option<oneshot::Sender<Instant>>,
}

impl<R> ShutdownReader<R> {
    fn new(inner: R, input_closed: oneshot::Sender<Instant>) -> Self {
        Self {
            inner,
            input_closed: Some(input_closed),
        }
    }

    fn report_input_closed(&mut self) {
        if let Some(input_closed) = self.input_closed.take() {
            let _ = input_closed.send(Instant::now());
        }
    }
}

impl<R: AsyncRead + Unpin> AsyncRead for ShutdownReader<R> {
    fn poll_read(
        mut self: Pin<&mut Self>,
        context: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let filled = buffer.filled().len();
        let had_capacity = buffer.remaining() > 0;
        let poll = Pin::new(&mut self.inner).poll_read(context, buffer);
        match poll {
            Poll::Ready(Ok(())) if had_capacity && buffer.filled().len() == filled => {
                self.report_input_closed();
                Poll::Ready(Ok(()))
            }
            Poll::Ready(Err(error)) => {
                self.report_input_closed();
                Poll::Ready(Err(error))
            }
            poll => poll,
        }
    }
}
