use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
#[cfg(unix)]
mod process_descriptors;
#[cfg(unix)]
mod process_exit;
#[cfg(unix)]
mod python;
mod python_requirement;
#[cfg(unix)]
mod r_bridge;
#[cfg(unix)]
mod r_environment;
#[cfg(unix)]
mod r_graphics;
mod r_package_name;
mod relay_protocol;
mod resolver;
mod sandbox;
mod server;
mod server_transport;
#[cfg(unix)]
mod sideband;
#[cfg(unix)]
mod sql;
mod transcript;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    match cli::Cli::parse().command {
        cli::Command::Serve {
            worker,
            relay,
            no_sandbox,
        } => match run_server(worker, relay, no_sandbox) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Worker => match worker::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::WorkerRelay { command } => match worker_relay::run(&command) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::SandboxManager {
            root_pid,
            cleanup_timeout_millis,
            temporary_directory,
        } => match sandbox::run_manager(root_pid, cleanup_timeout_millis, temporary_directory) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::SandboxTarget {
            signal_mask,
            command,
        } => match sandbox::run_target(signal_mask, &command) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox {
            exit_with_parent,
            command,
        } => match sandbox::run(&command, exit_with_parent) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
    }
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
    no_sandbox: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(target_os = "linux")]
    if !no_sandbox {
        return Err("Linux requires `mcp-console serve --no-sandbox`".into());
    }
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(worker, relay, no_sandbox));
    // `server::run` has already joined service and worker shutdown. Tokio's
    // stdout uses a blocking task that cannot be cancelled while the client
    // leaves its output pipe full, so runtime teardown must not wait for it.
    // The process exits immediately after this function returns.
    runtime.shutdown_background();
    result
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
