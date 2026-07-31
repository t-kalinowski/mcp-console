use std::error::Error;
use std::sync::{Arc, Mutex};

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, tool, tool_handler,
    tool_router, transport::stdio,
};
use serde::Deserialize;

use crate::ark::ArkKernel;

#[derive(Clone, Default)]
struct ConsoleServer {
    kernel: Arc<Mutex<Option<ArkKernel>>>,
}

#[derive(Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
struct SendRequest {
    /// Complete multiline R cell in persistent state.
    r: Option<String>,

    /// Exact text for an active R input prompt. A newline submits each input line.
    stdin: Option<String>,
}

#[tool_router]
impl ConsoleServer {
    #[tool(
        description = "Persistent R console. Send one complete r cell, or send stdin after the response ends with [input]."
    )]
    fn send(&self, Parameters(request): Parameters<SendRequest>) -> Result<String, String> {
        let operation = match (request.r, request.stdin) {
            (Some(code), None) => SendOperation::Evaluate(code),
            (None, Some(input)) => SendOperation::Input(input),
            (Some(_), Some(_)) => {
                return Err(String::from("send exactly one of r or stdin"));
            }
            (None, None) => {
                return Err(String::from("send exactly one of r or stdin"));
            }
        };

        let mut kernel = self
            .kernel
            .lock()
            .expect("ark kernel lock should be usable");
        match operation {
            SendOperation::Evaluate(code) => {
                let kernel = match kernel.as_mut() {
                    Some(kernel) => kernel,
                    None => kernel.insert(ArkKernel::start()?),
                };
                kernel.evaluate(code)
            }
            SendOperation::Input(input) => kernel
                .as_mut()
                .ok_or_else(|| String::from("stdin is accepted only at an R input prompt"))?
                .provide_input(input),
        }
    }
}

enum SendOperation {
    Evaluate(String),
    Input(String),
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

pub async fn run() -> Result<(), Box<dyn Error>> {
    let service = ConsoleServer::default().serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}
