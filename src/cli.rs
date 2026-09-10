use std::ffi::OsString;
use std::path::PathBuf;

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
    Serve {
        /// Run evaluated code with server permissions, without sandbox isolation or descendant cleanup
        #[arg(long)]
        no_sandbox: bool,

        /// Replace the runtime worker during development
        #[arg(long, hide = true, value_name = "PATH")]
        worker: Option<PathBuf>,

        /// Replace the worker relay during development
        #[arg(long, hide = true, value_name = "PATH", requires = "worker")]
        relay: Option<PathBuf>,
    },

    /// Run the internal R worker
    #[command(hide = true)]
    Worker,

    /// Run the internal worker relay
    #[command(hide = true)]
    WorkerRelay {
        /// Worker command to launch through the relay
        #[arg(
            value_name = "COMMAND",
            required = true,
            num_args = 1..,
            allow_hyphen_values = true,
            trailing_var_arg = true
        )]
        command: Vec<OsString>,
    },

    /// Run a command with the default or an explicit sandbox policy
    #[command(after_help = SANDBOX_EXAMPLES)]
    Sandbox {
        /// Read the runner configuration as JSON from this launch environment variable
        #[arg(long, value_name = "NAME", conflicts_with = "exit_with_parent")]
        config_env: Option<String>,

        /// Retire the sandbox when this parent process exits
        #[arg(long, hide = true, value_name = "PID")]
        exit_with_parent: Option<u32>,

        /// Command and arguments to run
        #[arg(
            value_name = "COMMAND",
            required = true,
            num_args = 1..,
            trailing_var_arg = true
        )]
        command: Vec<OsString>,
    },
}
