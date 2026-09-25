use std::process::ExitCode;

use clap::Parser;

mod cell;
mod cli;
mod config;
mod docker;
mod docker_sandbox;
#[cfg(unix)]
mod input_watch;
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
        #[cfg(unix)]
        cli::Command::InspectPython { executable } => match inspect_python_command(&executable) {
            Ok(configuration) => {
                println!("{configuration}");
                ExitCode::SUCCESS
            }
            Err(error) => exit_with_error(error),
        },
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
        cli::Command::Worker => match worker::run() {
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
        cli::Command::ImageRuntimeProbe => match target_launch::runtime::runtime_probe() {
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

#[cfg(unix)]
static INSPECTION_SIGNAL: std::sync::atomic::AtomicI32 = std::sync::atomic::AtomicI32::new(-1);

#[cfg(unix)]
extern "C" fn cancel_inspection_signal(_: libc::c_int) {
    use std::sync::atomic::Ordering;

    let descriptor = INSPECTION_SIGNAL.swap(-1, Ordering::Relaxed);
    if descriptor >= 0 {
        // SAFETY: write is async-signal-safe, and this handler sends one byte.
        unsafe { libc::write(descriptor, b"1".as_ptr().cast(), 1) };
    }
}

#[cfg(unix)]
fn inspect_python_command(executable: &std::path::Path) -> Result<String, String> {
    use std::io::{self, Read};
    use std::os::fd::AsRawFd as _;
    use std::sync::atomic::Ordering;

    let (mut notification, sender) = io::pipe().map_err(|error| error.to_string())?;
    INSPECTION_SIGNAL.store(sender.as_raw_fd(), Ordering::Relaxed);
    // The CLI process exits after this command. Its signal handler only
    // forwards cancellation to the existing resolver stop handle.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = cancel_inspection_signal as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        for signal in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
            if libc::sigaction(signal, &action, std::ptr::null_mut()) != 0 {
                return Err(io::Error::last_os_error().to_string());
            }
        }
    }
    let (handle_sender, handle_receiver) =
        std::sync::mpsc::channel::<resolver::ResolverStopHandle>();
    let watcher = std::thread::spawn(move || {
        let Ok(handle) = handle_receiver.recv() else {
            return;
        };
        let mut signal = [0];
        loop {
            match notification.read(&mut signal) {
                Ok(1) => {
                    let _ = handle.stop();
                    break;
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                _ => break,
            }
        }
    });
    let result = python::inspect_selected(executable, |handle| {
        handle_sender
            .send(handle)
            .map_err(|_| "Python inspection cancellation watcher stopped".to_string())
    });
    INSPECTION_SIGNAL.store(-1, Ordering::Relaxed);
    drop(handle_sender);
    drop(sender);
    watcher
        .join()
        .map_err(|_| "Python inspection cancellation watcher panicked".to_string())?;
    let configuration = result?;
    serde_json::to_string(&configuration).map_err(|error| error.to_string())
}

fn run_server(
    worker: Option<std::path::PathBuf>,
    relay: Option<std::path::PathBuf>,
    no_sandbox: bool,
    writable_roots: Vec<std::path::PathBuf>,
    overrides: &[String],
) -> Result<(), Box<dyn std::error::Error>> {
    let settings::Captured {
        source,
        policy,
        target,
        provider,
    } = settings::discover(overrides)?;
    if provider == settings::Provider::Compute {
        docker_sandbox::validate_policy(&policy, false, &writable_roots)?;
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
        sandbox::capture_policy(source, policy, writable_roots)?
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(server::run(worker, relay, no_sandbox, settings, target));
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
