//! The remote parent owns ordinary launcher lifetime, never native supervision.

use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use super::launch_io::{Io, duplicate, poll};
use super::{Bootstrap, Hello, Retired};

const RETIRE_TIMEOUT: Duration = Duration::from_secs(6);

pub(super) fn run() -> Result<(), String> {
    let mut confirmed = true;
    let result = launch(&mut confirmed);
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

fn launch(confirmed: &mut bool) -> Result<(), String> {
    let deadline = Instant::now() + super::SETUP_TIMEOUT;
    let mut input = Io::new(duplicate(0)?, None, Some(deadline))?;
    // No BufReader: consume exactly this frame, even if relay traffic arrives
    // in the same write. The copy task inherits every following byte.
    let bytes =
        super::read_payload(&mut input, super::MAX_BOOTSTRAP).map_err(|error| error.to_string())?;
    let bootstrap: Bootstrap = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid SSH bootstrap: {error}"))?;
    super::compatible(bootstrap.version, &bootstrap.build)?;
    if !bootstrap.workspace.starts_with('/') {
        return Err("target.workspace must be an absolute remote directory path".into());
    }
    let metadata = std::fs::metadata(&bootstrap.workspace).map_err(|error| {
        format!(
            "cannot access remote target.workspace '{}': {error}",
            bootstrap.workspace
        )
    })?;
    if !metadata.is_dir() {
        return Err(format!(
            "remote target.workspace '{}' is not a directory",
            bootstrap.workspace
        ));
    }
    std::env::set_current_dir(&bootstrap.workspace)
        .map_err(|error| format!("cannot enter remote target.workspace: {error}"))?;
    let executable = std::env::current_exe().map_err(|error| error.to_string())?;
    let mut policy = if bootstrap.no_sandbox {
        bootstrap.policy
    } else {
        crate::sandbox::materialize_settings(
            bootstrap.policy,
            bootstrap.writable_roots,
            std::path::Path::new(&bootstrap.workspace),
        )?
    };
    let mut command = Command::new(&executable);
    if bootstrap.no_sandbox {
        configure_direct_environment(&mut command, &policy)?;
    } else {
        command
            .args(["sandbox", "--exit-with-parent"])
            .arg(std::process::id().to_string())
            .args(["--settings-env", crate::settings::ENVIRONMENT, "--"]);
    }
    // These are capability controls, never controller interpreter selections.
    command
        .env("MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION", "0")
        .env("MCP_CONSOLE_PREINSTALLED", "1")
        .env("RETICULATE_USE_MANAGED_VENV", "no")
        .env_remove("MCP_CONSOLE_MANAGED_PYTHON")
        .env_remove(crate::settings::ENVIRONMENT);
    crate::settings::preserve_environment(&mut policy, command.get_envs())?;
    if !bootstrap.no_sandbox {
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
        supervise(probe, false, true, Some(deadline), confirmed)?;
    }
    let hello = serde_json::to_vec(&Hello {
        version: super::VERSION,
        build: env!("CARGO_PKG_VERSION").into(),
    })
    .map_err(|error| error.to_string())?;
    let mut output = Io::new(duplicate(1)?, None, Some(deadline))?;
    super::write_frame(&mut output, super::HELLO, &hello).map_err(|error| error.to_string())?;
    if !bootstrap.no_sandbox {
        command.arg(&executable);
    }
    command.arg("worker-relay").arg(&executable).arg("worker");
    supervise(command, true, !bootstrap.no_sandbox, None, confirmed)
}

fn configure_direct_environment(
    command: &mut Command,
    policy: &crate::settings::SandboxSettings,
) -> Result<(), String> {
    // The direct path has no native runner. Only its existing target-environment
    // controls apply; permission settings do not affect the SSH/helper process.
    #[derive(serde::Deserialize)]
    struct Environment {
        #[serde(default = "inherit_environment")]
        inherit_environment: bool,
        #[serde(default)]
        environment: std::collections::BTreeMap<String, String>,
    }
    fn inherit_environment() -> bool {
        true
    }
    let environment: Environment =
        serde_json::from_value(serde_json::Value::Object(policy.clone()))
            .map_err(|error| format!("invalid direct worker environment: {error}"))?;
    if !environment.inherit_environment {
        command.env_clear();
    }
    command.envs(environment.environment);
    Ok(())
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

fn supervise(
    mut command: Command,
    relay: bool,
    sandbox: bool,
    deadline: Option<Instant>,
    confirmed: &mut bool,
) -> Result<(), String> {
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
    let mut input_task = if relay {
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
            if !relay {
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
                return Err("SSH connection closed".into());
            }
            if events[2] != 0 {
                match input_task
                    .take()
                    .expect("input task is running")
                    .join()
                    .map_err(|_| "SSH input task panicked")?
                {
                    Ok(()) => return Err("SSH connection closed".into()),
                    // Relay stdin may close before its final output and exit.
                    // That is not EOF on the authenticated SSH input stream.
                    Err(error) if error.kind() == io::ErrorKind::BrokenPipe => {}
                    Err(error) => return Err(format!("SSH input forwarding failed: {error}")),
                }
            }
            if events[3] != 0 {
                output_task
                    .take()
                    .expect("output task is running")
                    .join()
                    .map_err(|_| "SSH output task panicked")??;
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
                return Err("SSH connection closed".into());
            }
            output_task
                .take()
                .expect("output task is running")
                .join()
                .map_err(|_| "SSH output task panicked")??;
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
