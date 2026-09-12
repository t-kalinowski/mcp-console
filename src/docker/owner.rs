//! A controller process owns one container independently of Docker attachment.
use super::process::{self, Cancel};
use crate::target_launch::transfer::{Io, duplicate, poll};
use crate::target_launch::{self, Bootstrap, Hello, Retired};
use serde::{Deserialize, Serialize};
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub session: super::Session,
    pub name: String,
    pub probe: bool,
    pub bootstrap: Bootstrap,
}

static SIGNAL: AtomicI32 = AtomicI32::new(-1);
extern "C" fn stop_signal(_: libc::c_int) {
    let fd = SIGNAL.load(Ordering::Relaxed);
    if fd >= 0 {
        unsafe {
            libc::write(fd, b"1".as_ptr().cast(), 1);
        }
    }
}

pub(super) fn run() -> Result<(), String> {
    let mut input = Io::new(
        duplicate(0)?,
        None,
        Some(Instant::now() + super::COMMAND_TIMEOUT),
    )?;
    let bytes = target_launch::read_payload(&mut input, super::LIMIT, super::PROTOCOL)
        .map_err(|e| e.to_string())?;
    let request: Request =
        serde_json::from_slice(&bytes).map_err(|e| format!("invalid Docker owner request: {e}"))?;
    let cancel = Cancel::new()?;
    let (signal, notify_signal) = io::pipe().map_err(|e| e.to_string())?;
    SIGNAL.store(notify_signal.as_raw_fd(), Ordering::Relaxed);
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = stop_signal as *const () as usize;
        libc::sigemptyset(&mut action.sa_mask);
        for signal in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
            if libc::sigaction(signal, &action, std::ptr::null_mut()) != 0 {
                return Err(io::Error::last_os_error().to_string());
            }
        }
    }
    let (done, completion) = io::pipe().map_err(|e| e.to_string())?;
    let watch_cancel = cancel.clone();
    let watcher = std::thread::spawn(move || {
        if let Ok(events) = poll(
            &[
                (signal.as_raw_fd(), libc::POLLIN),
                (done.as_raw_fd(), libc::POLLIN),
            ],
            None,
        ) && events[0] != 0
        {
            watch_cancel.cancel();
        }
    });
    let (input_done, input_completion) =
        std::os::unix::net::UnixStream::pair().map_err(|e| e.to_string())?;
    let input_watch = crate::input_watch::InputWatch::new(input_completion.as_raw_fd())?;
    let input_cancel = cancel.clone();
    let input_watcher = std::thread::spawn(move || {
        if input_watch.wait(input_completion).is_err() {
            input_cancel.cancel();
        }
    });
    let mut container = Container {
        session: &request.session,
        name: &request.name,
        id: None,
        retired: false,
    };
    let result = (|| {
        container.create(&cancel, request.probe)?;
        cancel.check()?;
        attach(&container, &request.bootstrap, &cancel)
    })();
    let cleanup = container.retire();
    let confirmed = cleanup.is_ok();
    let result = match (result, cleanup) {
        (Ok(()), result) | (result, Ok(())) => result,
        (Err(error), Err(cleanup)) => Err(format!("{error}; {cleanup}")),
    };
    let retired = serde_json::to_vec(&Retired {
        confirmed,
        error: result.as_ref().err().cloned(),
    })
    .map_err(|e| e.to_string())?;
    let mut output = Io::new(
        duplicate(1)?,
        None,
        Some(Instant::now() + Duration::from_secs(1)),
    )?;
    let delivered = target_launch::write_frame(&mut output, target_launch::RETIRED, &retired)
        .map_err(|e| e.to_string());
    drop(input_done);
    let _ = input_watcher.join();
    drop(completion);
    let _ = watcher.join();
    SIGNAL.store(-1, Ordering::Relaxed);
    // Once retirement has been reported, the frame carries workload errors;
    // this owner's exit status describes only cleanup and frame delivery.
    if confirmed { delivered } else { result }
}

struct Container<'a> {
    session: &'a super::Session,
    name: &'a str,
    id: Option<String>,
    retired: bool,
}

impl Container<'_> {
    fn create(&mut self, cancel: &Cancel, probe: bool) -> Result<(), String> {
        let crate::settings::Compute::Docker(docker) = &self.session.target.compute else {
            unreachable!()
        };
        let prefix = self.session.target.command();
        let mut command = self.session.endpoint.command();
        command.args([
            "container",
            "create",
            "--name",
            self.name,
            "--label",
            &format!("{}={}", super::LABEL, self.name),
            "--interactive",
            "--init",
            "--no-healthcheck",
            "--restart=no",
            "--network=bridge",
            "--stop-signal=SIGTERM",
            "--stop-timeout=1",
            "--workdir=/",
            "--entrypoint",
            &prefix[0],
        ]);
        for mount in &docker.mounts {
            command.arg("--mount").arg(mount.argument());
        }
        if let Some(user) = &docker.user {
            command.arg("--user").arg(user);
        }
        command
            .arg("--")
            .arg(&self.session.image)
            .args(&prefix[1..])
            .arg(if probe {
                "docker-probe"
            } else {
                "docker-launch"
            });
        let bytes = process::run(
            command,
            cancel,
            Some(Instant::now() + super::COMMAND_TIMEOUT),
            false,
            None,
        )?;
        let id = String::from_utf8(bytes)
            .map_err(|e| e.to_string())?
            .trim()
            .to_string();
        if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("Docker create did not return a full container ID".into());
        }
        self.id = Some(id);
        Ok(())
    }

    fn list(&self, cancel: &Cancel) -> Result<Vec<String>, String> {
        let filter = match &self.id {
            Some(id) => format!("id={id}"),
            None => format!("label={}={}", super::LABEL, self.name),
        };
        let mut command = self.session.endpoint.command();
        command.args([
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            &filter,
        ]);
        let bytes = process::run(
            command,
            cancel,
            Some(Instant::now() + Duration::from_secs(2)),
            false,
            None,
        )?;
        let text = String::from_utf8(bytes).map_err(|e| e.to_string())?;
        Ok(text.lines().map(str::to_string).collect())
    }

    fn retire(&mut self) -> Result<(), String> {
        if self.retired {
            return Ok(());
        }
        let cancel = Cancel::new()?;
        let result = (|| {
            if self.id.is_none() {
                let ids = self.list(&cancel)?;
                if ids.len() > 1 {
                    return Err("multiple containers have the ownership token".into());
                }
                self.id = ids.into_iter().next();
                if self.id.is_none() {
                    // A cancelled create request may still be in flight at the
                    // daemon. An empty listing cannot acknowledge that request.
                    return Err("container creation returned no identity; an empty listing cannot confirm retirement of an unacknowledged creation".into());
                }
            }
            if let Some(id) = &self.id {
                let mut stop = self.session.endpoint.command();
                stop.args([
                    "container",
                    "stop",
                    "--signal=SIGTERM",
                    "--timeout=1",
                    "--",
                    id,
                ]);
                let _ = process::run(
                    stop,
                    &cancel,
                    Some(Instant::now() + Duration::from_secs(2)),
                    false,
                    None,
                );
                let mut remove = self.session.endpoint.command();
                remove.args(["container", "rm", "--force", "--volumes", "--", id]);
                // Only a successful daemon query proving absence is the receipt.
                let removed = process::run(
                    remove,
                    &cancel,
                    Some(Instant::now() + Duration::from_secs(2)),
                    false,
                    None,
                );
                if !self.list(&cancel)?.is_empty() {
                    return Err(format!(
                        "container remains after removal: {}",
                        removed.err().unwrap_or_default()
                    ));
                }
            }
            Ok(())
        })();
        self.retired = true;
        result.map_err(|error: String| {
            format!(
                "Docker container '{}' ({}) retirement is unconfirmed: {error}",
                self.name,
                self.id.as_deref().unwrap_or("creation ID unavailable")
            )
        })
    }
}

impl Drop for Container<'_> {
    fn drop(&mut self) {
        let _ = self.retire();
    }
}

fn attach(container: &Container<'_>, bootstrap: &Bootstrap, cancel: &Cancel) -> Result<(), String> {
    let mut command: Command = container.session.endpoint.command();
    command.args([
        "container",
        "start",
        "--attach",
        "--interactive",
        "--",
        container.id.as_ref().expect("created ID"),
    ]);
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("cannot attach Docker container: {e}"))?;
    let (exited, notify_exit) = io::pipe().map_err(|e| e.to_string())?;
    let mut exit = crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
        drop(notify_exit)
    })?;
    let stdin = child.stdin.take().expect("attachment stdin");
    let stdout = child.stdout.take().expect("attachment stdout");
    let input_cancel = cancel.reader.try_clone().map_err(|e| e.to_string())?;
    let output_cancel = cancel.reader.try_clone().map_err(|e| e.to_string())?;
    let (output_finished, output_done) = io::pipe().map_err(|e| e.to_string())?;
    let request = target_launch::encode(bootstrap)?;
    let input_task = std::thread::spawn(move || -> Result<(), String> {
        let mut destination = Io::new(
            stdin,
            Some(input_cancel.try_clone().map_err(|e| e.to_string())?),
            None,
        )?;
        destination.write_all(&request).map_err(|e| e.to_string())?;
        let mut source = Io::new(duplicate(0)?, Some(input_cancel), None)?;
        io::copy(&mut source, &mut destination).map_err(|e| e.to_string())?;
        Ok(())
    });
    let output_exit = exited.try_clone().map_err(|e| e.to_string())?;
    let container_id = container.id.clone();
    let output_task = std::thread::spawn(move || -> Result<(), String> {
        let _done = output_done;
        let source = crate::process_output::RelayOutput::new(stdout, output_exit);
        let retirement = target_launch::Retirement::default();
        let mut source = target_launch::Output::new(source, super::PROTOCOL, retirement);
        let mut destination = Io::new(duplicate(1)?, Some(output_cancel), None)?;
        let hello = serde_json::to_vec(&Hello {
            container_id,
            version: target_launch::VERSION,
            build: env!("CARGO_PKG_VERSION").into(),
        })
        .map_err(|e| e.to_string())?;
        target_launch::write_frame(&mut destination, target_launch::HELLO, &hello)
            .map_err(|e| e.to_string())?;
        let mut buffer = [0; target_launch::MAX_FRAME];
        loop {
            let count = source.read(&mut buffer).map_err(|e| e.to_string())?;
            if count == 0 {
                return Ok(());
            }
            target_launch::write_frame(&mut destination, target_launch::DATA, &buffer[..count])
                .map_err(|e| e.to_string())?;
        }
    });
    let events = poll(
        &[
            (cancel.reader.as_raw_fd(), libc::POLLIN),
            (output_finished.as_raw_fd(), libc::POLLIN),
            (exited.as_raw_fd(), libc::POLLIN),
        ],
        None,
    )?;
    if events[0] != 0 || events[1] != 0 {
        // A malformed/closed stream is never an instruction to replay work.
        let _ = child.kill();
    } else {
        poll(
            &[
                (cancel.reader.as_raw_fd(), libc::POLLIN),
                (output_finished.as_raw_fd(), libc::POLLIN),
            ],
            None,
        )?;
    }
    cancel.cancel();
    if !exit.wait(Duration::from_secs(1))? {
        let _ = child.kill();
    }
    if !exit.wait(Duration::from_secs(1))? {
        return Err("Docker attachment did not exit".into());
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = input_task.join();
    let output = output_task
        .join()
        .map_err(|_| "Docker output task panicked")?;
    output?;
    if !status.success() {
        return Err(format!("Docker attachment exited with {status}"));
    }
    Ok(())
}
