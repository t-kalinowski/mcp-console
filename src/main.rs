use std::process::ExitCode;

use clap::Parser;

// Retain the library's build-script native link metadata in the executable.
use mcp_console as _;

#[cfg(windows)]
mod windows;

mod cell;
mod cli;
mod config;
mod console_paths;
#[cfg(unix)]
mod input_watch;
#[cfg(any(unix, windows))]
mod jsonl;
mod local_runtime;
#[cfg(unix)]
mod process_descriptors;
#[cfg(any(unix, windows))]
mod process_exit;
#[cfg(unix)]
mod process_io;
#[cfg(any(unix, windows))]
mod process_output;
#[cfg(any(unix, windows))]
mod python;
mod python_requirement;
#[cfg(any(unix, windows))]
mod r_bridge;
#[cfg(any(unix, windows))]
mod r_environment;
#[cfg(any(unix, windows))]
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
#[cfg(any(unix, windows))]
mod sideband;
#[cfg(any(unix, windows))]
mod sql;
mod transcript;
mod worker;
mod worker_client;
mod worker_protocol;
mod worker_relay;

fn main() -> ExitCode {
    let cli = cli::Cli::parse();
    let mut overrides = cli.overrides.values;
    match cli.command {
        #[cfg(windows)]
        cli::Command::SandboxSetup { status, state_dir } => {
            match sandbox::windows_setup(status, state_dir) {
                Ok(exit_code) => {
                    if !status && exit_code == ExitCode::SUCCESS {
                        println!("\n{}", cli::SANDBOX_SETUP_DETAILS);
                    }
                    exit_code
                }
                Err(error) => {
                    eprintln!("{error}");
                    ExitCode::FAILURE
                }
            }
        }
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
        cache,
        python,
        languages,
        source: _,
        mut policy,
        mut resolver,
    } = settings::discover(overrides)?;
    resolver::cache::configure(
        cache,
        no_sandbox,
        python.as_deref(),
        &mut resolver,
        &mut policy,
    )?;
    if python.is_some() && (worker.is_some() || relay.is_some()) {
        return Err("python selection requires the built-in worker and relay".into());
    }
    let settings = if no_sandbox {
        settings::SandboxSettings::default()
    } else {
        // Native validation belongs to the owned background launch. Running a
        // preflight child here would precede MCP serving and EOF ownership.
        sandbox::materialize_settings(policy, writable_roots, &std::env::current_dir()?)?
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(
        worker, relay, no_sandbox, settings, python, resolver, languages,
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
