use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use super::attachment::{self, Request};
use super::flow::Cursor;
use super::stream::{Destination, Engine, nonblocking};
use super::{Hello, READY, VERSION, challenge, invalid, status};
use crate::ssh::launch_io::{duplicate, poll};

pub(super) fn run(operation: &str) -> io::Result<()> {
    let target = std::env::var("MCP_CONSOLE_SSH_TARGET").map_err(|e| invalid(e.to_string()))?;
    let generation =
        std::env::var("MCP_CONSOLE_SSH_GENERATION").map_err(|e| invalid(e.to_string()))?;
    // Only this adapter retains controller state. No capability is put in its
    // environment, argv, metadata, project files, or transcript.
    unsafe {
        std::env::remove_var("MCP_CONSOLE_SSH_TARGET");
        std::env::remove_var("MCP_CONSOLE_SSH_GENERATION");
    }
    let target: crate::settings::SshTarget =
        serde_json::from_str(&target).map_err(|e| invalid(e.to_string()))?;
    let secret = challenge()?;
    let owner = challenge()?[..16]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    let hello = Hello {
        version: VERSION,
        build: env!("CARGO_PKG_VERSION").into(),
        operation: operation.into(),
        lease_ms: target.lease_ms,
        owner,
        generation: generation
            .parse()
            .map_err(|_| invalid("invalid SSH generation"))?,
    };
    hello.check(operation).map_err(invalid)?;
    let mut engine = Engine::new(false, target.lease_ms);
    engine.source = Some(duplicate(0).map_err(io::Error::other)?);
    engine.destination = Some(Destination::Local(status::Writer::new(
        duplicate(1).map_err(io::Error::other)?,
    )));
    engine.stderr = Some(duplicate(2).map_err(io::Error::other)?);
    for fd in [0, 1, 2] {
        nonblocking(fd)?;
    }
    let session = crate::ssh::Session::new(target, Vec::new());
    let (received, notification) = io::pipe()?;
    nonblocking(received.as_raw_fd())?;
    let mut received = File::from(std::os::fd::OwnedFd::from(received));
    let (results, completions) = mpsc::sync_channel(1);
    let mut attempt: Option<(thread::JoinHandle<()>, io::PipeWriter)> = None;
    let mut epoch = 0;
    let mut next_attempt = Instant::now();
    let mut backoff = Duration::from_millis(100);
    let result = (|| {
        loop {
            if engine.tick()? {
                return Ok(());
            }
            if engine.source_closed && engine.link.is_none() {
                return Err(io::Error::other(
                    "SSH shutdown during interruption; remote retirement is unconfirmed",
                ));
            }
            if engine.link.is_none() && attempt.is_none() && Instant::now() >= next_attempt {
                epoch += 1;
                let request = Request {
                    hello: hello.clone(),
                    epoch,
                    nonce: challenge()?,
                    create: epoch == 1,
                    secret: (epoch == 1).then_some(secret),
                };
                // Reconnection includes OpenSSH and the trusted command
                // prefix, just like initial setup. The remaining lease caps
                // that budget; authentication alone never extends it.
                let deadline = engine
                    .deadline
                    .min(Instant::now() + crate::ssh::SETUP_TIMEOUT);
                let (cancelled, cancel) = io::pipe()?;
                let session = session.clone();
                let results = results.clone();
                let mut notification = notification.try_clone()?;
                let task = thread::spawn(move || {
                    let result = (|| {
                        let mut link = attachment::connect(
                            &session,
                            &request,
                            &secret,
                            deadline,
                            cancelled.try_clone()?,
                        )?;
                        let (tag, bytes) = attachment::wait_ready(&mut link, deadline, &cancelled)?;
                        if tag != READY {
                            return Err(invalid("expected SSH owner resume cursor"));
                        }
                        let cursor: Cursor =
                            serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
                        {
                            let proof_time = link.proof_time;
                            Ok((link, cursor, proof_time))
                        }
                    })();
                    let _ = results.send(result);
                    let _ = notification.write_all(&[1]);
                });
                attempt = Some((task, cancel));
            }
            let mut descriptors = engine.descriptors();
            descriptors.push((received.as_raw_fd(), libc::POLLIN));
            let mut wake = engine.wake();
            if engine.link.is_none() && attempt.is_none() {
                wake = wake.min(next_attempt);
            }
            let events = match poll(&descriptors, Some(wake)) {
                Ok(events) => events,
                Err(_) if Instant::now() >= wake => continue,
                Err(error) => return Err(io::Error::other(error)),
            };
            engine.process(&events)?;
            if events[7] != 0 {
                received.read_exact(&mut [0])?;
                let (task, _cancel) = attempt.take().expect("SSH attachment attempt");
                task.join()
                    .map_err(|_| invalid("SSH attachment task panicked"))?;
                match completions
                    .recv()
                    .map_err(|_| invalid("SSH attachment result lost"))?
                {
                    Ok((link, cursor, started)) => {
                        engine.install(link, Some(cursor), started)?;
                        backoff = Duration::from_millis(100);
                    }
                    Err(error) if error.kind() == io::ErrorKind::InvalidData => return Err(error),
                    Err(_) => {
                        engine.status("recovering");
                        next_attempt = Instant::now() + backoff;
                        backoff = (backoff * 2).min(Duration::from_secs(1));
                    }
                }
            }
        }
    })();
    if let Some((task, cancel)) = attempt {
        drop(cancel);
        let _ = task.join();
    }
    if engine.output_abandoned {
        return Ok(());
    }
    if result.is_err() {
        engine.status("unconfirmed");
        if let Some(Destination::Local(writer)) = &mut engine.destination {
            let _ = writer.service();
        }
    } else if let Some(link) = &mut engine.link
        && let Some(mut child) = link.child.take()
    {
        let mut exit =
            crate::process_exit::ChildExitWaiter::start(child.id()).map_err(io::Error::other)?;
        if !exit
            .wait(Duration::from_secs(6))
            .map_err(io::Error::other)?
        {
            let _ = child.kill();
        }
        let status = child.wait()?;
        if !status.success() {
            return Err(io::Error::other(format!("SSH exited with {status}")));
        }
    }
    // Drops the attachment and all replay state when this live adapter stops.
    drop(notification);
    result
}
