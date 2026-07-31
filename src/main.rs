use std::process::ExitCode;

use clap::Parser;

mod ark;
mod cli;
mod sandbox;
mod server;
mod worker;

fn main() -> ExitCode {
    match cli::Cli::parse().command {
        cli::Command::Serve => match tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .expect("MCP runtime should start")
            .block_on(server::run())
        {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox { command } => match sandbox::run(&command) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Worker { connection_file } => match worker::run(&connection_file) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
    }
}

fn exit_with_error(error: impl std::fmt::Display) -> ExitCode {
    eprintln!("{error}");
    ExitCode::FAILURE
}
