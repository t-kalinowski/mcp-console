use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
mod python;
mod python_requirement;
#[cfg(target_os = "macos")]
mod r_bridge;
#[cfg(target_os = "macos")]
mod r_environment;
#[cfg(target_os = "macos")]
mod r_graphics;
mod relay_protocol;
mod resolver;
mod sandbox;
mod server;
mod server_transport;
#[cfg(target_os = "macos")]
mod sideband;
#[cfg(target_os = "macos")]
mod sql;
mod transcript;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    match cli::Cli::parse().command {
        cli::Command::Serve { worker, relay } => match run_server(worker, relay) {
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
        cli::Command::Sandbox { command } => match sandbox::run(&command) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
    }
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
) -> Result<(), Box<dyn std::error::Error>> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(server::run(worker, relay))
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
