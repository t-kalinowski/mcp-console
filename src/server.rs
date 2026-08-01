use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, schemars, tool, tool_handler,
    tool_router,
};
use serde::Deserialize;
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

const WORKER_SHUTDOWN_GRACE: Duration = Duration::from_secs(1);

#[derive(Clone)]
struct ConsoleServer {
    worker: crate::worker_client::Client,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SendArguments {
    /// Complete multiline R code evaluated in persistent state.
    r: Option<String>,
    /// Exact text supplied at this cell's first input request or an active [input]
    /// prompt. One value may satisfy multiple reads in the same evaluation. Each
    /// line is limited to 512 bytes, including its newline.
    stdin: Option<String>,
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>) -> Result<Self, String> {
        let worker = match worker {
            Some(program) => crate::worker_client::Client::new(program),
            None => crate::worker_client::Client::r()?,
        };
        Ok(Self { worker })
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(
        description = "Evaluate one complete R code cell in persistent state. Stdin may accompany the cell or follow an [input] response."
    )]
    async fn send(
        &self,
        Parameters(SendArguments { r, stdin }): Parameters<SendArguments>,
    ) -> Result<String, String> {
        match (r, stdin) {
            (Some(r), stdin) => self.worker.evaluate(r, stdin).await,
            (None, Some(stdin)) => self.worker.provide_input(stdin).await,
            (None, None) => Err("send requires r or stdin".to_string()),
        }
    }
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

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
