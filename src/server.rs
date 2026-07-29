use std::error::Error;
use std::sync::{Arc, Mutex};

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, schemars, tool, tool_handler,
    tool_router, transport::stdio,
};
use serde::Deserialize;

#[derive(Clone)]
struct ConsoleServer {
    worker: Arc<Mutex<crate::worker::RWorker>>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct ConsoleArguments {
    /// Complete multiline R code evaluated as top-level expressions.
    r: String,
}

impl ConsoleServer {
    fn new() -> Result<Self, Box<dyn Error>> {
        Ok(Self {
            worker: Arc::new(Mutex::new(crate::worker::RWorker::start()?)),
        })
    }
}

#[tool_router]
impl ConsoleServer {
    #[tool(description = "Evaluate one complete R code cell in persistent state.")]
    async fn send(
        &self,
        Parameters(ConsoleArguments { r }): Parameters<ConsoleArguments>,
    ) -> Result<String, String> {
        let worker = self.worker.clone();
        tokio::task::spawn_blocking(move || {
            let mut worker = worker
                .lock()
                .map_err(|_| "R worker lock poisoned".to_string())?;
            worker.evaluate(r)
        })
        .await
        .map_err(|error| format!("R worker task failed: {error}"))?
    }
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

pub async fn run() -> Result<(), Box<dyn Error>> {
    let service = ConsoleServer::new()?.serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}
