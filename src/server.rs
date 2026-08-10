use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
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
    /// Complete multiline Python code evaluated in persistent state. Matplotlib figures explicitly
    /// displayed with `pyplot.show()` or associated with a final Matplotlib figure, artist, or
    /// container expression while still pyplot-managed return as PNG images. Saving a figure to a
    /// file does not display it, and all pyplot-managed figures are closed at cell end. R plots
    /// invoked through reticulate's `r` bridge return under the same cell-scoped rules and R options
    /// as `r`. Omit to write stdin or poll.
    python: Option<String>,
    /// Complete DuckDB SQL evaluated in the persistent catalog. Omit to write stdin or poll.
    sql: Option<String>,
    /// Exact UTF-8 text queued to worker fd 0 without adding a newline.
    stdin: Option<String>,
    /// Maximum time this call waits for an evaluation. It does not limit or stop the computation.
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
struct PythonRequirements {
    /// One or more additive, single-line PEP 508 Python requirement strings.
    #[schemars(length(min = 1, max = 64), inner(length(min = 1)))]
    python: Vec<String>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SessionArguments {
    /// Prepare Python requirements or restart the implicit session, starting it if needed.
    action: SessionAction,
    /// Additive requirements for prepare. Omit for restart.
    requirements: Option<PythonRequirements>,
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>) -> Result<Self, String> {
        let transcript = crate::transcript::Transcript::create()?;
        let worker = match worker {
            Some(program) => crate::worker_client::Client::new(program),
            None => crate::worker_client::Client::builtin()?,
        };
        Ok(Self { worker, transcript })
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
        let (content, is_error) = response.into_parts();
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
                    result_images.push(artifact);
                    ContentBlock::image(data, mime_type)
                }
            })
            .collect();
        call.record_result_images(result_images)?;
        Ok(if is_error {
            CallToolResult::error(content)
        } else {
            CallToolResult::success(content)
        })
    }

    #[tool(
        description = "Prepare additive Python requirements before the implicit session starts, or restart its worker while retaining prepared requirements. Restart starts a worker if none exists and loses all in-memory R, Python, and SQL state."
    )]
    async fn session(
        &self,
        Parameters(SessionArguments {
            action,
            requirements,
        }): Parameters<SessionArguments>,
    ) -> Result<CallToolResult, String> {
        let text = match action {
            SessionAction::Prepare => {
                let Some(PythonRequirements { python }) = requirements else {
                    return Err("`requirements` is required with `prepare`".to_string());
                };
                validate_python_requirements(&python)?;
                match self.worker.prepare_python(python).await? {
                    crate::worker_client::PrepareResult::Prepared => "[prepared]",
                    crate::worker_client::PrepareResult::RestartRequired => "restart required",
                }
            }
            SessionAction::Restart => {
                if requirements.is_some() {
                    return Err("`requirements` is not yet supported with `restart`".to_string());
                }
                self.worker
                    .restart(Instant::now() + WORKER_SHUTDOWN_GRACE)
                    .await?;
                "[restarted]"
            }
        };
        Ok(CallToolResult::success(vec![ContentBlock::text(text)]))
    }
}

fn validate_python_requirements(python: &[String]) -> Result<(), String> {
    if python.is_empty() {
        return Err("`requirements.python` must contain at least one requirement".to_string());
    }
    if python.len() > 64 {
        return Err("`requirements.python` accepts at most 64 requirements".to_string());
    }
    if python.iter().any(String::is_empty) {
        return Err("Python requirement strings must not be empty".to_string());
    }
    if python.iter().any(|requirement| {
        requirement
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
    }) {
        return Err("Python requirement strings must not contain NUL or line breaks".to_string());
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
        let transcript = self.transcript.clone();
        let request_id = context.id.clone();
        let request_meta = context.meta.clone();
        let (request, call) = tokio::task::spawn_blocking(move || {
            let call = transcript.begin(&request_id, &request_meta, &request);
            (request, call)
        })
        .await
        .map_err(|error| {
            ErrorData::internal_error(format!("transcript task failed: {error}"), None)
        })?;
        let call = call.map_err(|error| ErrorData::internal_error(error, None))?;
        context.extensions.insert(call.clone());
        let result = Self::tool_router()
            .call(ToolCallContext::new(self, request, context))
            .await;
        let transcript = self.transcript.clone();
        let (result, recording) = tokio::task::spawn_blocking(move || {
            let recording = transcript.finish(call, &result);
            (result, recording)
        })
        .await
        .map_err(|error| {
            ErrorData::internal_error(format!("transcript task failed: {error}"), None)
        })?;
        recording.map_err(|error| {
            ErrorData::internal_error(
                format!("tool call completed but transcript recording failed: {error}"),
                None,
            )
        })?;
        result
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
    let service = server.serve((input, tokio::io::stdout())).await?;
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
