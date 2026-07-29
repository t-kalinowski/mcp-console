use std::error::Error;

use rmcp::{
    ServerHandler, ServiceExt, handler::server::wrapper::Parameters, model::JsonObject,
    serde_json::Value, tool, tool_handler, tool_router, transport::stdio,
};

#[derive(Clone)]
struct ConsoleServer;

#[tool_router]
impl ConsoleServer {
    #[tool(description = "Echo the supplied arguments.")]
    fn send(&self, Parameters(arguments): Parameters<JsonObject>) -> String {
        Value::Object(arguments).to_string()
    }
}

#[tool_handler(name = "mcp-console")]
impl ServerHandler for ConsoleServer {}

pub async fn run() -> Result<(), Box<dyn Error>> {
    let service = ConsoleServer.serve(stdio()).await?;
    service.waiting().await?;
    Ok(())
}
