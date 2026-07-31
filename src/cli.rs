use std::ffi::OsString;

use clap::{Parser, Subcommand};

const ROOT_EXAMPLES: &str = "\
Examples:
  mcp-console serve
  mcp-console sandbox -- python -c 'print(\"hello\")'";

const SANDBOX_EXAMPLES: &str = "\
Examples:
  mcp-console sandbox -- Rscript analysis.R
  mcp-console sandbox -- python script.py";

#[derive(Debug, Parser)]
#[command(
    name = "mcp-console",
    version,
    about = env!("CARGO_PKG_DESCRIPTION"),
    after_help = ROOT_EXAMPLES
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Run the MCP server over standard input and output
    Serve,

    /// Run a command with the MCP Console sandbox policy
    #[command(after_help = SANDBOX_EXAMPLES)]
    Sandbox {
        /// Command and arguments to run
        #[arg(
            value_name = "COMMAND",
            required = true,
            num_args = 1..,
            allow_hyphen_values = true,
            trailing_var_arg = true
        )]
        command: Vec<OsString>,
    },

    /// Run the private embedded Ark worker
    #[command(hide = true)]
    Worker {
        /// Jupyter connection file created by the MCP server
        #[arg(value_name = "CONNECTION_FILE")]
        connection_file: OsString,
    },
}
