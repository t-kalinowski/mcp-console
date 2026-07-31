use std::error::Error;
use std::path::PathBuf;
use std::pin::Pin;
use std::task::{Context, Poll};

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, model::JsonObject,
    serde_json::Value, tool, tool_handler, tool_router,
};
use tokio::io::{AsyncRead, ReadBuf};
use tokio::sync::oneshot;

#[derive(Clone)]
struct ConsoleServer {
    worker: Option<crate::worker_client::Client>,
}

impl ConsoleServer {
    fn new(worker: Option<PathBuf>) -> Self {
        Self {
            worker: worker.map(crate::worker_client::Client::new),
        }
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(description = "Echo the supplied arguments.")]
    async fn send(&self, Parameters(arguments): Parameters<JsonObject>) -> Result<String, String> {
        let Some(worker) = &self.worker else {
            return Ok(Value::Object(arguments).to_string());
        };
        let r = arguments
            .get("r")
            .and_then(Value::as_str)
            .filter(|_| arguments.len() == 1)
            .ok_or_else(|| "send exactly one r argument".to_string())?
            .to_string();
        worker.evaluate(r).await
    }
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

/// Runs the MCP stdio server and owns the selected development worker.
///
/// Closing MCP input also stops a worker whose evaluation is still running.
pub async fn run(worker: Option<PathBuf>) -> Result<(), Box<dyn Error>> {
    let server = ConsoleServer::new(worker);
    let worker = server.worker.clone();
    let (input_closed, wait_for_input_close) = oneshot::channel();
    let input = ShutdownReader::new(tokio::io::stdin(), input_closed);
    let service = server.serve((input, tokio::io::stdout())).await?;
    let shutdown = async move {
        let _ = wait_for_input_close.await;
        if let Some(worker) = worker {
            worker.shutdown().await?;
        }
        Ok::<(), String>(())
    };

    let (result, shutdown) = tokio::join!(service.waiting(), shutdown);
    shutdown.map_err(std::io::Error::other)?;
    result?;
    Ok(())
}

/// Reports EOF to the worker owner while otherwise behaving like its input.
struct ShutdownReader<R> {
    inner: R,
    input_closed: Option<oneshot::Sender<()>>,
}

impl<R> ShutdownReader<R> {
    fn new(inner: R, input_closed: oneshot::Sender<()>) -> Self {
        Self {
            inner,
            input_closed: Some(input_closed),
        }
    }

    fn report_input_closed(&mut self) {
        if let Some(input_closed) = self.input_closed.take() {
            let _ = input_closed.send(());
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
