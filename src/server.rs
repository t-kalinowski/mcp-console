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
    /// Complete multiline R cell evaluated in persistent global state. The default R environment
    /// includes tidyverse, reticulate, DBI, duckdb, and their full dependency sets, such as ggplot2,
    /// dplyr, readr, and jsonlite. Use other CRAN packages directly through `library()`, `require()`,
    /// `requireNamespace()`, `loadNamespace()`, `::`, or `:::`; the built-in worker resolves missing
    /// plain package names on demand and retains successful additions. Resolution makes a package
    /// available but does not attach it except through the original `library()` or `require()` call;
    /// `pkg::fun()` loads only the namespace as usual. Do not probe package availability or call
    /// `install.packages()`. Read
    /// Python globals through `py$name`; for example,
    /// `df <- tibble::as_tibble(py$df)`. R data frames are directly queryable by name from later SQL
    /// cells. Access DuckDB tables and views through the borrowed `sql_connection()` with DBI or
    /// dplyr; do not disconnect it. Default-device plots return as PNG images. Keep all drawing
    /// operations for one plot in the same cell. Set persistent dimensions with
    /// `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`;
    /// width and height are in inches. Omit to send stdin or poll.
    r: Option<String>,
    /// Complete multiline Python cell evaluated in persistent `__main__` state; its final expression
    /// is displayed. The built-in managed Python environment includes NumPy and pandas. If you use a
    /// custom Python installation, import packages already installed there directly. Use
    /// `requirements` on this call, or `session` ahead of time, to prepare other packages such as
    /// scikit-learn or Matplotlib. Read R globals and call R functions through `r.name`; for example,
    /// `frame = r.df`. Return Python globals to R through `py$name`. Python data frames are not
    /// automatically visible to SQL; bind them to an R name first. At cell end, including after a
    /// Python error, every open `matplotlib.pyplot` figure returns once as a PNG image and is closed.
    /// `show()` is optional. R plots called through `r` follow the R plot rules. Omit to send stdin or
    /// poll.
    python: Option<String>,
    /// Complete DuckDB SQL cell evaluated in the persistent catalog. Use it for filtering, joins,
    /// aggregation, and tabular inspection. An unqualified relation name can query a data frame in R
    /// global state; a DuckDB table or view with the same name takes precedence. Query results return
    /// a bounded preview. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN` for discovery.
    /// DuckDB CLI dot commands are not supported. Omit to send stdin or poll.
    sql: Option<String>,
    /// Additive R packages, Python packages, or DuckDB extensions to prepare before this cell and
    /// retain for later cells. Preparation does not import, attach, or load them. Ordinary CRAN
    /// packages used by the built-in R worker need not be declared here; use `requirements.r` to
    /// stage packages ahead of evaluation or provide explicit IR references. Python imports and SQL
    /// are not scanned. The cell is not run if preparation fails or further changes require restart.
    /// Resolution runs outside the worker sandbox and may download packages or extensions or execute
    /// installation or build code on the host. Use only trusted requirements. This field requires one
    /// `r`, `python`, or `sql` cell.
    requirements: Option<Requirements>,
    /// Text for interactive reads and debugger commands such as R `readline()` or `browser()` and
    /// Python `input()`, `breakpoint()`, or `pdb`. Its UTF-8 encoding is queued exactly; no newline is
    /// added. When sent with a cell, requirement preparation completes first, then nonempty text is
    /// queued before the code is run; an already waiting interactive read may consume it before the
    /// new cell begins. Empty text queues nothing. Send it on its own while the console is running or
    /// idle. If output ends in `[stdin needed]`, send the requested input here. Unread text can satisfy
    /// later reads and is discarded by restart.
    stdin: Option<String>,
    /// Maximum time this call waits once the cell has been dispatched, including one automatic
    /// worker replacement attempt. Requirement preparation happens first and may make the complete
    /// call take longer. Automatic R resolution is part of the running evaluation and counts toward
    /// this wait. On expiry, the call returns available output followed by the current state, such as
    /// `[running]` or `[worker starting]`, without stopping the computation or startup. Poll by
    /// calling `send` again without `r`, `python`, `sql`, or `stdin`.
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
    /// `polars>=1`, `scikit-learn`, or `matplotlib; python_version >= '3.10'`. Extras, version
    /// specifiers, and environment markers are accepted. Paths, file URLs, editable requirements,
    /// direct references, local archives, and local projects are rejected. An idle server-managed
    /// worker may activate compatible additions without losing state. A nonempty user-selected
    /// `RETICULATE_PYTHON` disables managed Python requirements.
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
    /// additions; omit `requirements` to restart unchanged. Requirements persist across restart but
    /// do not import, attach, or load packages or extensions. Successfully activated automatic R
    /// additions also persist across restart. After a recoverable live preparation or automatic R
    /// activation failure, evaluation remains available so state can be saved, but new requirement
    /// additions return `[restart required]` until restart. Resolution runs outside the worker
    /// sandbox and may download packages or extensions or execute package installation or build code
    /// on the host. Managed Python uses the server's startup resolver configuration; evaluated code
    /// cannot configure that host resolver. Managed Python startup and Matplotlib cache warming may
    /// also execute selected code on the host; use only trusted requirements.
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
        description = "Persistent mixed-language computational workbench. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. Choose the clearest language for each step and switch freely between calls. The default R environment includes tidyverse, reticulate, DBI, and duckdb, together with their full dependency sets, such as ggplot2, dplyr, readr, and jsonlite. The built-in managed Python environment includes NumPy and pandas. DuckDB SQL is also available. State persists across calls. Python reads R globals through `r.name`; R reads Python globals through `py$name`; SQL queries R data frames by name; R accesses the DuckDB catalog through `sql_connection()`. Language-native help and introspection are available. In the built-in worker, R package namespaces are resolved on demand when an R cell first uses a missing plain CRAN package through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`. Treat CRAN packages as available and use the packages best suited to the task; do not probe for installation or call `install.packages()`. Resolution is typically fast, especially after first use, because environments and package installations are cached. A lightweight best-effort scan batches obvious static package references before an R cell runs; dynamic uses resolve when reached. Automatic additions are additive and retained across cells and restart. Resolution makes a package available but attaches it only through the user's original `library()` or `require()` call; `pkg::fun()` loads only its namespace, as usual. Use `requirements.r` only to stage packages ahead of time or provide an explicit IR reference such as a non-CRAN source. Python imports and SQL are not scanned. Explicit preparation does not load, import, or attach packages or extensions. If you use a custom Python installation, import packages already installed there directly. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Send exactly one complete `r`, `python`, or `sql` cell. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` for interactive reads or debugger commands; omit code and stdin to poll an active evaluation or immediately collect output produced while the worker is idle. A wait timeout applies after a cell is dispatched and does not stop computation. Explicit requirement preparation happens first and can make the complete call take longer. Dynamic R resolution is part of the running evaluation, so the wait can return `[running]` while its resolver continues; `session(action = \"interrupt\")` targets that resolver. Running work must be collected before new code is sent. R errors, Python exceptions, and DuckDB errors are ordinary console output, so inspect result text and continue or correct the cell. Evaluated code can read host files but cannot directly access the network and can write only within the worker's private temporary directory. Automatic R resolution and managed Python resolution triggered by evaluated R code are host-side exceptions: they may access the network and execute installation or build code, so use only trusted packages. Automatic R discovery accepts only plain package names; use explicit `requirements.r` for other IR references. Managed Python accepts only named registry requirements. Managed Python version requests accept version numbers and supported PEP 440 comparison specifiers, not interpreter selectors. Python resolution uses the server's startup configuration; changes to `UV_*` made by evaluated code do not configure it. Starting the server with a nonempty user-selected `RETICULATE_PYTHON` disables managed Python requirement additions. Package availability and system compatibility can still produce ordinary installation or load errors."
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
        description = "Make additional R or Python packages and DuckDB extensions available, request SIGINT for an active host resolver or otherwise send it to the live worker, or restart the persistent console session. The built-in worker prepares DuckDB's JSON and ICU extensions by default. In the built-in worker, ordinary plain CRAN packages resolve automatically when used by R. Use `prepare` to stage R packages ahead of a cell, supply explicit IR references such as supported remote sources, prepare Python packages, or add DuckDB extensions. Preparation does not import, attach, or load packages or extensions. An idle worker can add R requirements or DuckDB extensions without losing live state; compatible Python additions require a server-managed worker. Requirements and successfully activated automatic R additions are additive, idempotent, and persist across restart. After a recoverable live preparation or automatic R activation failure, evaluation remains available so state can be saved, but new requirement additions require restart. Managed Python accepts named PEP 508 registry requirements only; paths, file URLs, editable requirements, direct references, local archives, and local projects are rejected. Managed Python resolution uses the server's startup `UV_*` configuration, which evaluated code cannot change. A nonempty user-selected `RETICULATE_PYTHON` disables managed Python requirements. `interrupt` returns after targeting an active automatic R or other host resolver, or sending the signal to the live worker; user code may catch or delay it. `restart` may optionally add R, Python, and DuckDB requirements, then replaces the worker and loses all in-memory R, Python, and SQL state, debugger state, and unread stdin. Requirement resolution runs outside the execution sandbox and may download packages or extensions or execute package installation or build code on the host; use only trusted requirements. Automatic R discovery accepts only plain package names; use explicit `requirements.r` for other IR references."
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
