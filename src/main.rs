use std::ffi::OsStr;
use std::process::ExitCode;

use clap::Parser;

mod cli;
mod sandbox;
mod server;
#[cfg(target_os = "macos")]
mod sideband;
mod worker;

#[tokio::main(flavor = "current_thread")]
async fn main() -> ExitCode {
    let arguments = std::env::args_os().skip(1).collect::<Vec<_>>();
    if matches!(arguments.as_slice(), [mode] if mode == OsStr::new("__worker_bootstrap")) {
        return match worker::bootstrap() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        };
    }
    if matches!(arguments.as_slice(), [mode] if mode == OsStr::new("__worker")) {
        return match worker::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        };
    }

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
