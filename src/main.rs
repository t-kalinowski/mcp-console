use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
mod python;
#[cfg(target_os = "macos")]
mod r_bridge;
mod sandbox;
mod server;
#[cfg(target_os = "macos")]
mod sideband;
#[cfg(target_os = "macos")]
mod sql;
mod worker;
mod worker_client;
#[cfg(target_os = "macos")]
mod worker_protocol;

fn main() -> ExitCode {
    match cli::Cli::parse().command {
        cli::Command::Serve { worker } => match run_server(worker) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Worker => match worker::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox { command } => match sandbox::run(&command) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
    }
}

fn run_server(worker: Option<std::path::PathBuf>) -> Result<(), Box<dyn std::error::Error>> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    runtime.block_on(server::run(worker))
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
