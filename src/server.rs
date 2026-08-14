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
    /// dplyr, readr, and jsonlite. Packages are not attached automatically. Read Python globals
    /// through `py$name`; for example,
    /// `df <- tibble::as_tibble(py$df)`. R data frames are directly queryable by name from later SQL
    /// cells. Access DuckDB tables and views through the borrowed `sql_connection()` with DBI or
    /// dplyr; do not disconnect it. Default-device plots return as PNG images. Keep all drawing
    /// operations for one plot in the same cell. Set persistent dimensions with
    /// `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`;
    /// width and height are in inches. Omit to send stdin or poll.
    r: Option<String>,
    /// Complete multiline Python cell evaluated in persistent `__main__` state; its final expression
    /// is displayed. The built-in managed Python environment includes NumPy and pandas. Use
    /// `session` to prepare other packages such as scikit-learn or Matplotlib. Read R globals and
    /// call R functions through `r.name`; for example, `frame = r.df`. Return Python globals to R
    /// through `py$name`. Python data frames are not automatically visible to SQL; bind them to an R
    /// name first. At cell end, including after a Python error, every open `matplotlib.pyplot` figure
    /// returns once as a PNG image and is closed. `show()` is optional. R plots called through `r`
    /// follow the R plot rules. Omit to send stdin or poll.
    python: Option<String>,
    /// Complete DuckDB SQL cell evaluated in the persistent catalog. Use it for filtering, joins,
    /// aggregation, and tabular inspection. An unqualified relation name can query a data frame in R
    /// global state; a DuckDB table or view with the same name takes precedence. Query results return
    /// a bounded preview. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, and `EXPLAIN` for discovery.
    /// DuckDB CLI dot commands are not supported. Omit to send stdin or poll.
    sql: Option<String>,
    /// Text for interactive reads and debugger commands such as R `readline()` or `browser()` and
    /// Python `input()`, `breakpoint()`, or `pdb`. Its UTF-8 encoding is queued to worker stdin
    /// exactly; no newline is added. Send it with a cell to prequeue input or on its own while the
    /// worker is running or idle. If output ends in `[stdin needed]`, send the requested input here.
    /// Unread text can satisfy later reads and is discarded by restart.
    stdin: Option<String>,
    /// Maximum time this call waits for an evaluation or one automatic worker replacement attempt.
    /// On expiry, the call returns available output followed by the current state, such as
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
    /// Additive, single-line IR package references for `prepare`, for example `data.table`, `sf`, or
    /// `yaml12`. An idle server-managed worker can add R requirements without losing live state.
    /// Local package sources are rejected because resolution runs with server permissions.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    r: Vec<String>,
    /// Additive, single-line PEP 508 requirements for `prepare` or `restart`, for example `polars>=1`,
    /// `scikit-learn`, or `matplotlib`. An idle server-managed worker may activate compatible
    /// additions without losing state.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    python: Vec<String>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SessionArguments {
    /// `prepare` adds R or Python requirements before a server-managed worker starts. After startup,
    /// it can add R and compatible Python requirements while the worker is idle. `interrupt`
    /// requests SIGINT for an active host resolver, or otherwise sends it to the live worker.
    /// `restart` replaces the worker, optionally adds Python requirements, and starts it if needed.
    action: SessionAction,
    /// Additive packages to make available. `prepare` requires at least one R or Python entry.
    /// `interrupt` accepts no requirements. `restart` accepts Python entries only; omit
    /// `requirements` to restart unchanged. Requirements persist across restart but do not import or
    /// attach packages. After a recoverable live
    /// preparation failure, evaluation remains available so state can be saved, but new requirement
    /// additions return `[restart required]` until restart. The same marker follows a failed
    /// automatic replacement. Resolution runs outside the worker sandbox and may download packages
    /// or execute installation or build code on the host. Managed Python startup and Matplotlib
    /// cache warming also run on the host and may execute selected code; use only trusted
    /// requirements.
    requirements: Option<Requirements>,
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>) -> Result<Self, String> {
        let transcript = crate::transcript::Transcript::new();
        let worker = match worker {
            Some(program) => crate::worker_client::Client::new(program),
            None => crate::worker_client::Client::builtin()?,
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
        description = "Persistent mixed-language computational workbench. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. Choose the clearest language for each step and switch freely between calls. The default R environment includes tidyverse, reticulate, DBI, and duckdb, together with their full dependency sets, such as ggplot2, dplyr, readr, and jsonlite. The built-in managed Python environment includes NumPy and pandas. DuckDB SQL is also available. State persists across calls. Python reads R globals through `r.name`; R reads Python globals through `py$name`; SQL queries R data frames by name; R accesses the DuckDB catalog through `sql_connection()`. Language-native help and introspection are available. Use `session` to prepare other packages before loading or importing them. R default-device plots and open `matplotlib.pyplot` figures return as PNG images. Send exactly one complete `r`, `python`, or `sql` cell. Call `send` sequentially; concurrent calls are unsupported. Use `stdin` for interactive reads or debugger commands; omit code and stdin to poll. A wait timeout does not stop computation, and running work must be collected before new code is sent. R errors, Python exceptions, and DuckDB errors are ordinary console output, so inspect result text and continue or correct the cell. Evaluated code can read host files but cannot directly access the network and can write only within the worker's private temporary directory. Managed Python requirement resolution triggered by R code such as `reticulate::py_require()` or by an R package load is a host-side exception: it may access the network and execute installation or build code, so use only trusted requirements."
    )]
    async fn send(
        &self,
        Extension(call): Extension<crate::transcript::Call>,
        Extension(delivery): Extension<crate::server_transport::ResponseDeliveryCall>,
        Parameters(SendArguments {
            r,
            python,
            sql,
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
        let response = self
            .worker
            .send(
                cell,
                stdin,
                Duration::from_millis(timeout_ms),
                self.transcript.clone(),
                call.id(),
            )
            .await;
        Ok(response_to_tool_result(
            response,
            &call,
            &self.transcript,
            &delivery,
        ))
    }

    #[tool(
        description = "Make additional R or Python packages available, request SIGINT for an active host resolver or send it to a live worker, or restart the persistent console session. Use `prepare` for packages not included in the built-in environments. Packages are not imported or attached automatically. An idle server-managed worker can add R and compatible Python requirements without losing live state. After a recoverable live preparation failure, evaluation remains available so state can be saved, but new requirement additions require restart. Requirements are additive, idempotent, and persist across restart. `interrupt` returns after sending the request or signal; user code may catch or delay it. `restart` may optionally add Python requirements, then replaces the worker and loses all in-memory R, Python, and SQL state, debugger state, and unread stdin. Requirement resolution runs outside the execution sandbox and may download packages or execute installation or build code on the host; use only trusted requirements."
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
        let text = match action {
            SessionAction::Prepare => {
                let Some(Requirements { r, python }) = requirements else {
                    return Err("`requirements` is required with `prepare`".to_string());
                };
                if r.is_empty() && python.is_empty() {
                    return Err(
                        "at least one of `requirements.r` or `requirements.python` is required"
                            .to_string(),
                    );
                }
                validate_r_requirements(&r)?;
                validate_python_requirements(&python)?;
                match self
                    .worker
                    .prepare(crate::worker_client::Requirements { r, python })
                    .await?
                {
                    crate::worker_client::PrepareResult::Prepared => "[prepared]",
                    crate::worker_client::PrepareResult::RestartRequired => "[restart required]",
                    crate::worker_client::PrepareResult::WorkerStopped(response) => {
                        return Ok(response_to_tool_result(
                            response,
                            &call,
                            &self.transcript,
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
                let python = match requirements {
                    Some(Requirements { r, python }) => {
                        if !r.is_empty() {
                            return Err(
                                "`requirements.r` is not supported with `restart`".to_string()
                            );
                        }
                        if python.is_empty() {
                            return Err(
                                "`requirements.python` must contain at least one requirement"
                                    .to_string(),
                            );
                        }
                        validate_python_requirements(&python)?;
                        python
                    }
                    None => Vec::new(),
                };
                let response = self.worker.restart(python, WORKER_SHUTDOWN_GRACE).await?;
                return Ok(response_to_tool_result(
                    response,
                    &call,
                    &self.transcript,
                    &delivery,
                ));
            }
        };
        Ok(CallToolResult::success(vec![ContentBlock::text(text)]))
    }
}

fn response_to_tool_result(
    response: crate::worker_client::Response,
    call: &crate::transcript::Call,
    transcript: &crate::transcript::Transcript,
    delivery: &crate::server_transport::ResponseDeliveryCall,
) -> CallToolResult {
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
        delivery.register(response_delivery);
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
    validate_requirements(python, "python", "Python")
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
        Arc::into_inner(result).expect("transcript task should release the tool result")
    }
}

/// Runs the MCP stdio server and owns the selected worker.
///
/// Closing MCP input also stops a worker whose evaluation is still running.
pub async fn run(worker: Option<PathBuf>) -> Result<(), Box<dyn Error>> {
    let server = ConsoleServer::new(worker).map_err(std::io::Error::other)?;
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
