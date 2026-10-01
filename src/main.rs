use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
mod config;
mod console_paths;
mod docker;
mod docker_sandbox;
#[cfg(unix)]
mod input_watch;
#[cfg(unix)]
mod jsonl;
#[cfg(unix)]
mod local_runtime;
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
mod target_launch;
mod target_session;
mod transcript;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    let cli = cli::Cli::parse();
    let mut overrides = cli.overrides.values;
    match cli.command {
        cli::Command::Serve {
            worker,
            relay,
            no_sandbox,
            writable_root,
            overrides: command_overrides,
        } => {
            overrides.extend(command_overrides.values);
            match run_server(worker, relay, no_sandbox, writable_root, &overrides) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::Worker { bootstrap_runtimes } => match worker::run(bootstrap_runtimes) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::Resolve => match resolver::run() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::DockerSandboxOwner => match docker_sandbox::run_owner() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::DockerSandboxLaunch => {
            match target_launch::run(docker_sandbox::PROTOCOL, false, Some("docker_sandbox")) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::DockerSandboxProbe => {
            match target_launch::run(docker_sandbox::PROTOCOL, true, Some("docker_sandbox")) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::DockerOwner => match docker::run_owner() {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => exit_with_error(error),
        },
        cli::Command::DockerLaunch => {
            match target_launch::run(docker::PROTOCOL, false, Some("docker")) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::DockerProbe => {
            match target_launch::run(docker::PROTOCOL, true, Some("docker")) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
        cli::Command::ImageRuntimeProbe { python } => {
            match target_launch::runtime::runtime_probe(python.as_deref()) {
                Ok(()) => ExitCode::SUCCESS,
                Err(error) => exit_with_error(error),
            }
        }
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
            overrides: command_overrides,
        } => {
            overrides.extend(command_overrides.values);
            match sandbox::run(
                &command,
                exit_with_parent,
                config_env.as_deref(),
                settings_env.as_deref(),
                writable_root,
                &overrides,
            ) {
                Ok(exit_code) => exit_code,
                Err(error) => exit_with_error(error),
            }
        }
    }
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
    no_sandbox: bool,
    writable_roots: Vec<std::path::PathBuf>,
    overrides: &[String],
) -> Result<(), Box<dyn std::error::Error>> {
    let settings::Captured {
        python,
        source,
        policy,
        target,
        provider,
    } = settings::discover(overrides)?;
    if provider == settings::Provider::Compute {
        docker_sandbox::validate_policy(&policy, false, &writable_roots)?;
    }
    if python.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("python selection requires the built-in worker and relay".into());
    }
    let target = target.map(|target| (target, writable_roots.clone()));
    if target.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("Execution targets require the built-in worker and relay".into());
    }
    let settings = if target.is_some() {
        policy
    } else if no_sandbox {
        settings::SandboxSettings::default()
    } else {
        sandbox::capture_policy(source.as_deref(), policy, writable_roots)?
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(
        worker, relay, no_sandbox, settings, target, python,
    ));
    // `server::run` has already finished owned runtime retirement and response settling. Tokio's
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
