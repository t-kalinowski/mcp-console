use std::ffi::OsString;
use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};

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
    #[command(flatten)]
    pub overrides: ConfigOverrides,

    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Args)]
pub struct ConfigOverrides {
    /// Override project configuration; repeat for multiple dotted KEY=VALUE assignments
    #[arg(short = 'c', long = "config", value_name = "KEY=VALUE")]
    pub values: Vec<String>,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    /// Run the MCP server over standard input and output
    Serve {
        #[command(flatten)]
        overrides: ConfigOverrides,

        /// Skip inner native enforcement; retain any selected Docker container or Sandbox microVM and its provider policy
        #[arg(long)]
        no_sandbox: bool,

        /// Allow writes to an additional path (temporary launch option)
        #[arg(long, value_name = "PATH", conflicts_with = "no_sandbox")]
        writable_root: Vec<PathBuf>,

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

    #[command(hide = true)]
    DockerOwner,
    #[command(hide = true)]
    DockerLaunch,
    #[command(hide = true)]
    DockerProbe,
    #[command(hide = true)]
    ImageRuntimeProbe,
    #[command(hide = true)]
    DockerSandboxOwner,
    #[command(hide = true)]
    DockerSandboxLaunch,
    #[command(hide = true)]
    DockerSandboxProbe,

    /// Launch the built-in runtime for an authenticated SSH controller
    #[command(hide = true)]
    SshLaunch,

    /// Prepare dependencies for an authenticated SSH controller
    #[command(hide = true)]
    SshPrepare,

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
        #[command(flatten)]
        overrides: ConfigOverrides,

        /// Read the runner configuration as JSON from this launch environment variable
        #[arg(long, value_name = "NAME", conflicts_with = "exit_with_parent")]
        config_env: Option<String>,

        /// Consume the application settings captured by the server
        #[arg(long, hide = true, value_name = "NAME", conflicts_with_all = ["config_env", "writable_root"])]
        settings_env: Option<String>,

        /// Allow writes to an additional path (temporary launch option)
        #[arg(long, value_name = "PATH", conflicts_with = "config_env")]
        writable_root: Vec<PathBuf>,

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
