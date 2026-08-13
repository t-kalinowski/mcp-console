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
    /// Complete multiline R code evaluated in persistent state. Plots drawn on the default device
    /// return as PNG images in MCP results. Set their size with the R options
    /// `options(console.plot.width = ..., console.plot.height = ..., console.plot.dpi = ...)`;
    /// width and height are in inches. Keep each plot and its drawing operations in one cell. Omit
    /// to write stdin or poll.
    r: Option<String>,
    /// Complete multiline Python code evaluated in persistent state. At cell end, including after a
    /// Python error, every open `matplotlib.pyplot` figure returns once as a PNG image and is closed;
    /// `show()` is optional, and `savefig()` does not suppress capture while the figure remains open.
    /// R plots invoked through reticulate's `r` bridge follow the cell-scoped rules and R options
    /// described for `r`. Omit to write stdin or poll.
    python: Option<String>,
    /// Complete DuckDB SQL evaluated in the persistent catalog. Omit to write stdin or poll.
    sql: Option<String>,
    /// Exact UTF-8 text queued to worker fd 0 without adding a newline.
    stdin: Option<String>,
    /// Maximum time this call waits for evaluation or one automatic worker replacement attempt.
    /// Expiry reports the current state without stopping the computation or startup.
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
enum SessionAction {
    Prepare,
    Restart,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct Requirements {
    /// One or more additive, single-line R package references accepted by IR for prepare.
    /// IR prevents installation from local package sources because it runs with server permissions.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    r: Vec<String>,
    /// One or more additive, single-line PEP 508 Python requirement strings for prepare or restart.
    #[serde(default)]
    #[schemars(length(max = 64), inner(length(min = 1)))]
    python: Vec<String>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SessionArguments {
    /// Prepare R or Python requirements or restart the implicit session, starting it if needed.
    action: SessionAction,
    /// Additive R or Python requirements for prepare.
    /// After startup, an idle server-managed worker can apply Python-only additions.
    /// A new R addition requires restart and applies none of that call's additions.
    /// New additions also require restart after an automatic replacement attempt fails.
    /// Restart accepts only Python requirements.
    /// Resolution runs outside the worker sandbox.
    /// Package installation, build code, managed Python startup, or Matplotlib
    /// cache warming may execute selected code on the host.
    /// Omit to restart unchanged.
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
        description = "Evaluate one complete R, Python, or SQL code cell, write its stdin, or poll it."
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
        description = "Prepare additive R or Python requirements, or restart the implicit session. An idle server-managed worker applies Python-only additions without losing state; new R additions require restart. Restart retains requirements and loses all in-memory R, Python, and SQL state."
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
