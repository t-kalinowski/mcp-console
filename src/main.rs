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
mod r_package_name;
mod relay_protocol;
mod resolver;
mod sandbox;
mod server;
mod server_transport;
#[cfg(any(target_os = "macos", target_os = "linux"))]
#[cfg_attr(target_os = "linux", allow(dead_code))]
mod sideband;
#[cfg(target_os = "macos")]
mod sql;
mod transcript;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    #[cfg(target_os = "macos")]
    if let Err(error) = configure_child_reaping() {
        return exit_with_error(error);
    }

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

#[cfg(target_os = "macos")]
fn configure_child_reaping() -> Result<(), String> {
    // An ignored signal disposition survives exec. Restore SIGCHLD before any
    // command starts managed children so their exit statuses remain waitable.
    // SAFETY: zeroed sigaction storage is initialized below before use.
    let mut action = unsafe { std::mem::zeroed::<libc::sigaction>() };
    action.sa_sigaction = libc::SIG_DFL;
    // SAFETY: action.sa_mask points to initialized writable storage.
    unsafe { libc::sigemptyset(&mut action.sa_mask) };
    // SAFETY: action is fully initialized and the old action is not requested.
    if unsafe { libc::sigaction(libc::SIGCHLD, &action, std::ptr::null_mut()) } == 0 {
        Ok(())
    } else {
        Err(format!(
            "failed to configure child-process reaping: {}",
            std::io::Error::last_os_error()
        ))
    }
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
) -> Result<(), Box<dyn std::error::Error>> {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(worker, relay));
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
