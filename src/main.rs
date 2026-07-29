use std::process::ExitCode;

use clap::Parser;

mod cli;
mod sandbox;
mod server;

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    match cli::Cli::parse().command {
        cli::Command::Serve => match server::run().await {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox { command } => match sandbox::run(&command) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
    }
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
