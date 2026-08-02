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
const DEFAULT_TIMEOUT_MS: u64 = 60_000;

#[derive(Clone)]
struct ConsoleServer {
    worker: crate::worker_client::Client,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SendArguments {
    /// Complete multiline R code evaluated in persistent state. Omit to poll a running cell.
    #[schemars(transform = disallow_null)]
    #[serde(default, deserialize_with = "deserialize_r")]
    r: Option<String>,
    /// Maximum time this call waits. It does not limit or stop the computation.
    #[schemars(transform = remove_format)]
    #[serde(default = "default_timeout_ms")]
    timeout_ms: u64,
}

fn default_timeout_ms() -> u64 {
    DEFAULT_TIMEOUT_MS
}

fn deserialize_r<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    String::deserialize(deserializer).map(Some)
}

fn remove_format(schema: &mut schemars::Schema) {
    schema.remove("format");
}

fn disallow_null(schema: &mut schemars::Schema) {
    let object = schema.ensure_object();
    object.remove("default");
    let Some(serde_json::Value::Array(types)) = object.get_mut("type") else {
        return;
    };

    types.retain(|value| value != "null");
    if types.len() == 1 {
        let only_type = types.pop().expect("one type should remain");
        object.insert("type".to_owned(), only_type);
    }
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
    #[tool(description = "Evaluate one complete R code cell or poll its running evaluation.")]
    async fn send(
        &self,
        Parameters(SendArguments { r, timeout_ms }): Parameters<SendArguments>,
    ) -> Result<String, String> {
        self.worker.send(r, Duration::from_millis(timeout_ms)).await
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
