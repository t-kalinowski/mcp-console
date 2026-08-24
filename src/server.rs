use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use rmcp::{
    RoleServer, ServerHandler, ServiceExt,
    handler::server::{common::Extension, tool::ToolCallContext, wrapper::Parameters},
    model::{CallToolRequestParams, CallToolResult, ContentBlock, ErrorData},
    schemars,
    service::RequestContext,
    tool, tool_handler, tool_router,
};
use serde::Deserialize;
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);
const DEFAULT_TIMEOUT_MS: u64 = 60_000;

#[derive(Clone)]
struct ConsoleServer {
    worker: crate::worker_client::Client,
    transcript: crate::transcript::Transcript,
    deliveries: crate::server_transport::ResponseDeliveries,
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
    /// DuckDB table or view with the same name takes precedence. Use `SHOW TABLES`, `DESCRIBE`,
    /// `SUMMARIZE`, and `EXPLAIN` for discovery. DuckDB CLI dot commands are not supported. Omit this
    /// field for polling or stdin-only calls.
    sql: Option<String>,
    /// Additive R packages, Python packages, or DuckDB extensions to prepare before this cell and
    /// retain for later cells. Requirements are additive and persist for the session. Preparation does
    /// not import, attach, or load them. Ordinary CRAN packages used by the built-in R worker need not
    /// be declared here; use `requirements.r` to stage packages ahead of evaluation or provide explicit
    /// IR references. In the built-in managed Python environment, missing imports normally resolve at
    /// runtime. Use `requirements.python` to stage a distribution before the cell, provide a version,
    /// extra, or marker, or correct automatic inference. Python source is not pre-scanned, and SQL does
    /// not trigger package discovery. The cell is not run if explicit preparation fails or further
    /// changes require restart. Resolution runs outside the worker sandbox and may download packages
    /// or extensions or execute installation or build code on the host. Use only trusted requirements.
    /// This field requires one `r`, `python`, or `sql` cell.
    requirements: Option<Requirements>,
    /// Input for an active read, prompt, or debugger. When responding to active input, omit R, Python,
    /// and SQL code and send stdin on its own. Its UTF-8 encoding is queued exactly; no newline is added.
    /// Line-oriented input therefore normally needs a trailing `\n`. When sent with requirements and a
    /// cell, requirement preparation completes first, then nonempty text is queued before the code is
    /// run; an already waiting interactive read may consume it before the new cell begins. Empty text
    /// queues nothing. If output ends in `[waiting for stdin]`, send the requested input here. Unread text
    /// can satisfy later reads and is discarded by restart.
    stdin: Option<String>,
    /// Maximum time this call waits once a cell has been dispatched or the call has attached to an
    /// active evaluation, including one automatic worker replacement attempt. Reaching the timeout
    /// does not cancel evaluation, resolution, or startup. Requirement preparation happens first and
    /// may make the complete call take longer. Automatic R and Python import resolution are part of
    /// the running evaluation and count toward this wait. On expiry, the call returns available output
    /// and a state marker, such as `[running; poll with an empty send]` or `[worker starting]`. If
    /// evaluation remains active, poll with an empty `send` call; do not resubmit the cell.
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(rename_all = "snake_case")]
enum SessionAction {
    Prepare,
    Interrupt,
    Restart,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[schemars(inline)]
#[serde(deny_unknown_fields)]
struct Requirements {
    /// Additive DuckDB extension names for `send`, `prepare`, or `restart`, for example `fts`,
    /// `spatial`, or `excel`. JSON and ICU are already prepared for built-in workers. Names must
    /// start with a lowercase ASCII letter and contain only lowercase ASCII letters, digits, and
    /// underscores. The host resolver uses DuckDB's own `INSTALL` outside the sandbox, with
    /// DuckDB's default extension repository and native cache. Preparation does not load extension
    /// code; `LOAD` and automatic loading happen later inside the sandbox.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1, max = 64)))]
    duckdb: Vec<String>,
    /// Additive, single-line IR package references for `send`, `prepare`, or `restart`, for example
    /// `data.table`, `sf`, or `yaml12`. Use this field to stage packages ahead of evaluation or supply
    /// an explicit supported remote IR reference. Automatic R discovery accepts only plain package
    /// names. An idle worker that implements R preparation can add requirements without losing live
    /// state. Local package sources are rejected because resolution runs with server permissions.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    r: Vec<String>,
    /// Additive, named PEP 508 registry requirements for `send`, `prepare`, or `restart`, for example
    /// `polars>=1`, `scikit-learn`, or `matplotlib; python_version >= '3.10'`. Use explicit
    /// requirements when automatic import inference needs a different distribution, a version, an
    /// extra, or an environment marker, or when the distribution should be prepared before the cell.
    /// Automatic imports infer bare distribution names only. Paths, file URLs, editable requirements,
    /// direct references, local archives, and local projects are rejected. Preparation does not import
    /// the package. An idle server-managed worker may activate compatible additions without losing
    /// state. A nonempty user-selected `RETICULATE_PYTHON` disables automatic resolution and managed
    /// Python requirements.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    python: Vec<String>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SessionArguments {
    /// `prepare` adds R or Python requirements or DuckDB extensions before a worker starts.
    /// After startup, it can add R requirements or DuckDB extensions while the worker is idle;
    /// compatible Python additions require a server-managed worker. `interrupt` requests SIGINT for
    /// an active host resolver, or otherwise sends it to the live worker. `restart` can add any of
    /// the same requirements before it replaces the worker and starts it if needed.
    action: SessionAction,
    /// Additive packages or DuckDB extensions to make available. `prepare` requires at least one R,
    /// Python, or DuckDB entry. `interrupt` accepts no requirements. `restart` accepts the same
    /// additions; omit `requirements` to restart unchanged. Requirements are additive and persist
    /// across restart but do not import, attach, or load packages or extensions. Successfully activated
    /// automatic R and Python additions also persist across restart. After a recoverable live
    /// preparation or automatic R activation failure, evaluation remains available so state can be
    /// saved, but new requirement additions return `[restart required]` until restart. Resolution runs
    /// outside the worker sandbox and may download packages or extensions or execute package
    /// installation or build code on the host. Managed Python uses the server's startup resolver
    /// configuration; evaluated code cannot configure that host resolver. Managed Python resolution,
    /// startup, and Matplotlib cache warming may also execute selected code on the host; use only
    /// trusted requirements.
    requirements: Option<Requirements>,
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>, relay: Option<PathBuf>) -> Result<Self, String> {
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
        })
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(
        description = r#"Persistent R, Python, and DuckDB SQL workbench for exact computation, file and data inspection, transformation, visualization, statistics, simulation, and modeling. State persists across sequential calls; choose the clearest language for each step and switch between calls. Python reads R globals through `r.name`, R reads Python globals through `py$name`, SQL can query R data frames by name, and R accesses DuckDB through `sql_connection()`.

Send one complete `r`, `python`, or `sql` cell per call. Calls must be sequential because only one evaluation can be active. When an intermediate result affects the next step, inspect it before sending another cell. R and Python display a final visible top-level expression, and SQL returns a bounded preview, so leave the primary result last and print only when additional output is needed. Cells are not transactional; changes made before an error may remain.

`timeout_ms` limits how long the call waits after dispatch or attachment; explicit requirement preparation can make the complete call take longer. It does not cancel startup, dependency resolution, or evaluation. If a response ends in `[running; poll with an empty send]`, call `send` again without code or stdin; do not resubmit the cell. Send `stdin` without code to answer an active prompt or debugger. Use `session(action = "interrupt")` to request interruption.

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
        if requirements.is_some() && cell.is_none() {
            return Err("`requirements` requires a code cell".to_string());
        }
        if let Some(requirements) = requirements.as_ref() {
            validate_environment_requirements(requirements)?;
        }
        let requirements = requirements.map(|Requirements { duckdb, r, python }| {
            crate::worker_client::Requirements { duckdb, r, python }
        });
        let response = self
            .worker
            .send(
                cell,
                stdin,
                requirements,
                Duration::from_millis(timeout_ms),
                self.transcript.clone(),
                call.id(),
            )
            .await;
        Ok(response_to_tool_result(
            response,
            &call,
            &self.transcript,
            &self.deliveries,
            &delivery,
        ))
    }

    #[tool(
        description = r#"Manage dependencies and the lifecycle of the persistent worker. `prepare` makes additional R or Python requirements or DuckDB extensions available without evaluating a cell. `interrupt` requests SIGINT for the active host resolver or live worker and returns after sending the request; interruption is cooperative, so if an evaluation remains active, use an empty `send` afterward to observe whether it stopped. `restart` optionally prepares requirements, replaces the worker, and discards all in-memory R, Python, SQL, debugger, and unread-stdin state.

Requirements are additive, idempotent, and persist across restart. Preparation does not import, attach, or load packages or extensions. Use `restart` only when clean state or a restart-required dependency change is needed; ordinary language errors normally leave the worker reusable. Dependency resolution runs outside the sandbox and may execute installation or build code; use only trusted requirements."#
    )]
    async fn session(
        &self,
        Extension(call): Extension<crate::transcript::Call>,
        Extension(delivery): Extension<crate::server_transport::ResponseDeliveryCall>,
        Parameters(SessionArguments {
            action,
            requirements,
        }): Parameters<SessionArguments>,
    ) -> Result<CallToolResult, String> {
        if let Some(requirements) = requirements.as_ref() {
            validate_environment_requirements(requirements)?;
        }
        let text = match action {
            SessionAction::Prepare => {
                let Some(Requirements { duckdb, r, python }) = requirements else {
                    return Err("`requirements` is required with `prepare`".to_string());
                };
                match self
                    .worker
                    .prepare(crate::worker_client::Requirements { duckdb, r, python })
                    .await?
                {
                    crate::worker_client::PrepareResult::Prepared => "[prepared]",
                    crate::worker_client::PrepareResult::RestartRequired => "[restart required]",
                    crate::worker_client::PrepareResult::Failed(response) => {
                        return Ok(response_to_tool_result(
                            response,
                            &call,
                            &self.transcript,
                            &self.deliveries,
                            &delivery,
                        ));
                    }
                    crate::worker_client::PrepareResult::WorkerStopped(response) => {
                        return Ok(response_to_tool_result(
                            response,
                            &call,
                            &self.transcript,
                            &self.deliveries,
                            &delivery,
                        ));
                    }
                }
            }
            SessionAction::Interrupt => {
                if requirements.is_some() {
                    return Err("`requirements` is not supported with `interrupt`".to_string());
                }
                self.worker.interrupt().await?;
                "[interrupt sent]"
            }
            SessionAction::Restart => {
                let Requirements { duckdb, r, python } = requirements.unwrap_or(Requirements {
                    duckdb: Vec::new(),
                    r: Vec::new(),
                    python: Vec::new(),
                });
                let response = self
                    .worker
                    .restart(
                        crate::worker_client::Requirements { duckdb, r, python },
                        WORKER_SHUTDOWN_GRACE,
                    )
                    .await?;
                return Ok(response_to_tool_result(
                    response,
                    &call,
                    &self.transcript,
                    &self.deliveries,
                    &delivery,
                ));
            }
        };
        Ok(CallToolResult::success(vec![ContentBlock::text(text)]))
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

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {
    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        mut context: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, ErrorData> {
        let request_id = context.id.clone();
        if !matches!(request.name.as_ref(), "send" | "session") {
            return Self::tool_router()
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
            Self::tool_router()
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
