use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
#[cfg(unix)]
mod process_descriptors;
#[cfg(unix)]
mod process_exit;
#[cfg(unix)]
mod process_output;
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
#[cfg(unix)]
mod readiness;
mod relay_protocol;
mod resolver;
mod sandbox;
mod server;
mod server_transport;
mod settings;
#[cfg(unix)]
mod sideband;
#[cfg(unix)]
mod sql;
mod ssh;
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
            writable_root,
        } => match run_server(worker, relay, no_sandbox, writable_root) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Worker => match worker::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::SshLaunch => match ssh::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::SshPrepare => match ssh::preparation::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::WorkerRelay { command } => match worker_relay::run(&command) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Sandbox {
            exit_with_parent,
            command,
            config_env,
            settings_env,
            writable_root,
        } => match sandbox::run(
            &command,
            exit_with_parent,
            config_env.as_deref(),
            settings_env.as_deref(),
            writable_root,
        ) {
            Ok(exit_code) => exit_code,
            Err(error) => exit_with_error(error),
        },
    }
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
    no_sandbox: bool,
    writable_roots: Vec<std::path::PathBuf>,
) -> Result<(), Box<dyn std::error::Error>> {
    let (source, policy, target) = settings::discover()?;
    let ssh = target.map(|target| ssh::Session::new(target, writable_roots.clone()));
    if ssh.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("SSH targets require the built-in worker and relay".into());
    }
    let settings = if ssh.is_some() {
        policy
    } else if no_sandbox {
        settings::SandboxSettings::default()
    } else {
        sandbox::capture_policy(source, policy, writable_roots)?
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(worker, relay, no_sandbox, settings, ssh));
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
