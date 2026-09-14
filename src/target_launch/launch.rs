//! The remote parent owns ordinary launcher lifetime, never native supervision.

use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use super::transfer::{Io, duplicate, poll};
use super::{Bootstrap, Hello, Retired};

const RETIRE_TIMEOUT: Duration = Duration::from_secs(6);

pub(super) fn run(
    protocol: super::Protocol,
    probe: bool,
    compute: Option<&'static str>,
) -> Result<(), String> {
    let mut confirmed = true;
    let result = launch(&mut confirmed, protocol, probe, compute);
    // Setup diagnostics stay on stderr. This terminal frame also distinguishes
    // a rejected launch from a broken connection with an unknown remote lifetime.
    let retired = serde_json::to_vec(&Retired {
        confirmed,
        error: result.as_ref().err().cloned(),
    })
    .map_err(|error| error.to_string())?;
    let mut output = Io::new(
        duplicate(1)?,
        None,
        Some(Instant::now() + Duration::from_secs(1)),
    )?;
    let delivered = super::write_frame(&mut output, super::RETIRED, &retired)
        .map_err(|error| format!("cannot report remote retirement: {error}"));
    match (result, delivered) {
        (Ok(()), result) | (result, Ok(())) => result,
        (Err(error), Err(delivery)) => Err(format!("{error}; {delivery}")),
    }
}

fn launch(
    confirmed: &mut bool,
    protocol: super::Protocol,
    probe_only: bool,
    compute: Option<&'static str>,
) -> Result<(), String> {
    let deadline = Instant::now() + super::SETUP_TIMEOUT;
    let mut input = Io::new(duplicate(0)?, None, Some(deadline))?;
    // No BufReader: consume exactly this frame, even if relay traffic arrives
    // in the same write. The copy task inherits every following byte.
    let bytes = super::read_payload(&mut input, super::MAX_BOOTSTRAP, protocol)
        .map_err(|error| error.to_string())?;
    let bootstrap: Bootstrap = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid {} bootstrap: {error}", protocol.0))?;
    protocol.compatible(bootstrap.version, &bootstrap.build)?;
    if bootstrap.provider == crate::settings::Provider::Compute {
        if compute != Some("docker_sandbox") {
            return Err("compute enforcement requires Docker Sandbox execution".into());
        }
        crate::docker_sandbox::validate_policy(
            &bootstrap.policy,
            false,
            &bootstrap.writable_roots,
        )?;
    }
    let native = bootstrap.provider.needs_native_runner(bootstrap.no_sandbox);
    super::enter_workspace(&bootstrap.workspace)?;
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let mut policy = if !native {
        bootstrap.policy
    } else {
        crate::sandbox::materialize_settings(
            bootstrap.policy,
            bootstrap.writable_roots,
            std::path::Path::new(&bootstrap.workspace),
        )?
    };
    let mut command = Command::new(&executable);
    if !native {
        super::WorkloadEnvironment::from_policy(&policy)
            .map_err(|error| format!("invalid direct worker environment: {error}"))?
            .configure(&mut command);
    } else {
        command
            .args(["sandbox", "--exit-with-parent"])
            .arg(std::process::id().to_string())
            .args(["--settings-env", crate::settings::ENVIRONMENT, "--"]);
    }
    if let Some(compute) = compute {
        super::runtime::configure_runtime(&mut command, &policy, compute)?;
        if let Some(environment) = &bootstrap.environment {
            environment.configure(&mut command)?;
        }
    } else if let Some(environment) = &bootstrap.environment {
        environment.configure(&mut command)?;
    } else {
        // Private launch-only callers select a bare runtime explicitly.
        command
            .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "0")
            .env("RETICULATE_USE_MANAGED_VENV", "no")
            .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
            .env_remove("MCP_CONSOLE_PYTHON_EXECUTABLE")
            .env_remove("MCP_CONSOLE_PREINSTALLED");
    }
    command.env_remove(crate::settings::ENVIRONMENT);
    crate::settings::preserve_environment(&mut policy, command.get_envs())?;
    if native {
        command.env(
            crate::settings::ENVIRONMENT,
            serde_json::to_string(&policy).map_err(|error| error.to_string())?,
        );
        let mut probe = Command::new(&executable);
        probe.args(command.get_args()).arg("/usr/bin/true");
        for (key, value) in command.get_envs() {
            match value {
                Some(value) => {
                    probe.env(key, value);
                }
                None => {
                    probe.env_remove(key);
                }
            }
        }
        supervise(
            probe,
            LaunchMode::Preflight,
            true,
            Some(deadline),
            confirmed,
            protocol,
        )?;
    }
    let hello = serde_json::to_vec(&Hello {
        container_id: None,
        sandbox: None,
        version: super::VERSION,
        build: env!("CARGO_PKG_VERSION").into(),
    })
    .map_err(|error| error.to_string())?;
    let mut output = Io::new(duplicate(1)?, None, Some(deadline))?;
    super::write_frame(&mut output, super::HELLO, &hello).map_err(|error| error.to_string())?;
    if probe_only {
        if native {
            command.arg(&executable);
        }
        command.arg("image-runtime-probe");
        return supervise(
            command,
            LaunchMode::Probe,
            native,
            Some(deadline),
            confirmed,
            protocol,
        );
    }
    if native {
        command.arg(&executable);
    }
    command.arg("worker-relay").arg(&executable).arg("worker");
    supervise(
        command,
        LaunchMode::Relay,
        native,
        None,
        confirmed,
        protocol,
    )
}

struct Owner {
    child: Child,
    exit: crate::process_exit::ChildExitWaiter,
    reaped: bool,
    sandbox: bool,
}

impl Owner {
    fn retire(&mut self) -> Result<(), String> {
        if self.reaped {
            return Ok(());
        }
        // The native launcher handles SIGTERM. A direct relay instead needs
        // stdin closure to run its existing bounded direct-worker retirement.
        if self.sandbox {
            unsafe {
                libc::kill(self.child.id() as libc::pid_t, libc::SIGTERM);
            }
        }
        if !self.exit.wait(RETIRE_TIMEOUT)? {
            self.child.kill().map_err(|error| error.to_string())?;
            if !self.exit.wait(Duration::from_secs(1))? {
                return Err("remote launcher did not exit after forced termination".into());
            }
            let _ = self.child.wait();
            self.reaped = true;
            return Err(
                "remote launcher required forced termination; retirement is unconfirmed".into(),
            );
        }
        self.reap()
    }

    fn reap(&mut self) -> Result<(), String> {
        let status = self.child.wait().map_err(|error| error.to_string())?;
        self.reaped = true;
        if status.success() {
            Ok(())
        } else {
            Err(format!("remote launcher exited with {status}"))
        }
    }
}

impl Drop for Owner {
    fn drop(&mut self) {
        let _ = self.retire();
    }
}

#[derive(Clone, Copy)]
enum LaunchMode {
    Preflight,
    Probe,
    Relay,
}

fn supervise(
    mut command: Command,
    mode: LaunchMode,
    sandbox: bool,
    deadline: Option<Instant>,
    confirmed: &mut bool,
    protocol: super::Protocol,
) -> Result<(), String> {
    let label = protocol.0;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command
        .spawn()
        .map_err(|error| format!("cannot launch remote runtime: {error}"))?;
    drop(command);
    *confirmed = false;
    let (exited, notify_exit) = io::pipe().map_err(|error| error.to_string())?;
    let exit = crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
        drop(notify_exit)
    })?;
    let stdin = child.stdin.take().expect("piped remote launcher stdin");
    let stdout = child.stdout.take().expect("piped remote launcher stdout");
    let mut owner = Owner {
        child,
        exit,
        reaped: false,
        sandbox,
    };
    let (cancel_input, input_cancel) = io::pipe().map_err(|error| error.to_string())?;
    let (input_finished, input_done) = io::pipe().map_err(|error| error.to_string())?;
    let mut input_task = if matches!(mode, LaunchMode::Relay) {
        let mut source = Io::new(
            duplicate(0)?,
            Some(cancel_input.try_clone().map_err(|e| e.to_string())?),
            None,
        )?;
        let mut destination = Io::new(stdin, Some(cancel_input), None)?;
        Some(thread::spawn(move || {
            let _done = input_done;
            io::copy(&mut source, &mut destination).map(|_| ())
        }))
    } else {
        drop(stdin);
        drop(input_done);
        None
    };
    let (cancel_output, output_cancel) = io::pipe().map_err(|error| error.to_string())?;
    let (output_finished, output_done) = io::pipe().map_err(|error| error.to_string())?;
    let output_exit = exited.try_clone().map_err(|error| error.to_string())?;
    let output_task = thread::spawn(move || -> Result<(), String> {
        let _done = output_done;
        let mut source = crate::process_output::RelayOutput::new(stdout, output_exit);
        let mut destination = Io::new(duplicate(1)?, Some(cancel_output), None)?;
        let mut bytes = [0; super::MAX_FRAME];
        loop {
            let count = source.read(&mut bytes).map_err(|error| error.to_string())?;
            if count == 0 {
                return Ok(());
            }
            if matches!(mode, LaunchMode::Preflight) {
                return Err("unexpected stdout during remote sandbox preflight".into());
            }
            super::write_frame(&mut destination, super::DATA, &bytes[..count])
                .map_err(|error| error.to_string())?;
        }
    });
    let mut output_task = Some(output_task);
    let result = (|| {
        loop {
            let events = poll(
                &[
                    (0, 0),
                    (exited.as_raw_fd(), libc::POLLIN),
                    (
                        if input_task.is_some() {
                            input_finished.as_raw_fd()
                        } else {
                            -1
                        },
                        libc::POLLIN,
                    ),
                    (
                        if output_task.is_some() {
                            output_finished.as_raw_fd()
                        } else {
                            -1
                        },
                        libc::POLLIN,
                    ),
                ],
                deadline,
            )?;
            if events[0] != 0 {
                return Err(format!("{label} connection closed"));
            }
            if events[2] != 0 {
                match input_task
                    .take()
                    .expect("input task is running")
                    .join()
                    .map_err(|_| format!("{label} input task panicked"))?
                {
                    Ok(()) => return Err(format!("{label} connection closed")),
                    // Relay stdin may close before its final output and exit.
                    // That is not EOF on the target connection input stream.
                    Err(error) if error.kind() == io::ErrorKind::BrokenPipe => {}
                    Err(error) => return Err(format!("{label} input forwarding failed: {error}")),
                }
            }
            if events[3] != 0 {
                output_task
                    .take()
                    .expect("output task is running")
                    .join()
                    .map_err(|_| format!("{label} output task panicked"))??;
            }
            if events[1] != 0 {
                break;
            }
        }
        owner.reap()?;
        *confirmed = true;
        // Finish queued output under normal backpressure, while still observing
        // connection closure independently of a blocked stdout write.
        if output_task.is_some() {
            let events = poll(&[(0, 0), (output_finished.as_raw_fd(), libc::POLLIN)], None)?;
            if events[0] != 0 {
                return Err(format!("{label} connection closed"));
            }
            output_task
                .take()
                .expect("output task is running")
                .join()
                .map_err(|_| format!("{label} output task panicked"))??;
        }
        Ok(())
    })();
    drop(input_cancel);
    if let Some(task) = input_task {
        let _ = task.join();
    }
    if !owner.reaped {
        *confirmed = owner.retire().is_ok();
    }
    drop(output_cancel);
    if let Some(task) = output_task {
        let _ = task.join();
    }
    result
}
