\name{mcp_console_tool}
\alias{mcp_console_tool}
\title{Use MCP Console as an ellmer tool}
\usage{
mcp_console_tool(from = "mcp-console")
}
\arguments{
\item{from}{A Python package requirement passed to
\code{reticulate::uv_run_tool()}. The default resolves the latest available
\code{mcp-console} package; use a requirement such as
\code{"mcp-console==0.0.2"} to pin a version.}
}
\value{
An \code{ellmer::ToolDef} that can be passed to
\code{chat$register_tool()}.
}
\description{
Resolves the \code{mcp-console} executable, starts
\code{mcp-console serve}, and returns its \code{send} tool as an ellmer tool.
The server process and its persistent R, Python, and DuckDB state live as long
as the returned tool remains reachable.
}
\examples{
\dontrun{
chat <- ellmer::chat_openai()
chat$register_tool(mcp_console_tool())
chat$chat("Use the console to simulate data and summarize the result.")
}
}
