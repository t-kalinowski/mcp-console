use std::collections::VecDeque;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use super::attachment::{self, Link, Request};
use super::stream::{Destination, Engine, nonblocking, would_block, write};
use super::{FAILED, HELLO, RETIREMENT, frame, invalid, json, read};
use crate::ssh::launch_io::{Io, duplicate, poll};

fn directory(request: &Request) -> PathBuf {
    PathBuf::from(format!("/tmp/mcp-console-{}", request.hello.owner))
}
struct Directory(PathBuf);
impl Drop for Directory {
    fn drop(&mut self) {
        let _ = fs::remove_file(self.0.join("socket"));
        let _ = fs::remove_dir(&self.0);
    }
}

pub(super) fn tunnel(operation: &str) -> io::Result<()> {
    let deadline = Instant::now() + crate::ssh::SETUP_TIMEOUT;
    let mut input = Io::new(
        duplicate(0).map_err(io::Error::other)?,
        None,
        Some(deadline),
    )
    .map_err(invalid)?;
    let mut output = Io::new(
        duplicate(1).map_err(io::Error::other)?,
        None,
        Some(deadline),
    )
    .map_err(invalid)?;
    let result = (|| {
        let (tag, bytes) = read(&mut input)?;
        if tag != HELLO {
            return Err(invalid("expected SSH ownership bootstrap"));
        }
        let mut request: Request = serde_json::from_slice(&bytes)
            .map_err(|e| invalid(format!("invalid SSH ownership bootstrap: {e}")))?;
        request.hello.check(operation).map_err(invalid)?;
        if request.epoch == 0
            || request.create != request.secret.is_some()
            || (request.create && request.epoch != 1)
        {
            return Err(invalid("invalid SSH create or attach request"));
        }
        if request.create {
            let mut command = Command::new(std::env::current_exe()?);
            command
                .arg("ssh-owner")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)
                .map_err(invalid)?;
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() < 0 {
                        Err(io::Error::last_os_error())
                    } else {
                        Ok(())
                    }
                });
            }
            let mut child = command.spawn()?;
            // The capability is private startup input, never an argument or file.
            let mut bootstrap = Io::new(
                child.stdin.take().expect("owner bootstrap"),
                None,
                Some(deadline),
            )
            .map_err(invalid)?;
            bootstrap.write_all(&frame(HELLO, &bytes))?;
            drop(bootstrap);
            let mut ready = Io::new(
                child.stdout.take().expect("owner ready pipe"),
                None,
                Some(deadline),
            )
            .map_err(invalid)?;
            ready.read_exact(&mut [0])?;
            // This disposable forwarding process is not the owner's lifetime
            // parent. It must not kill the owner when its SSH attachment exits.
            thread::spawn(move || {
                let _ = child.wait();
            });
        }
        let socket = UnixStream::connect(directory(&request).join("socket"))
            .map_err(|error| invalid(format!("SSH owner is missing or unavailable; prior effects and retirement are unconfirmed: {error}")))?;
        request.secret = None;
        let stream = socket.try_clone()?;
        let mut writer = Io::new(stream, None, Some(deadline)).map_err(invalid)?;
        writer.write_all(&frame(HELLO, &json(&request)))?;
        Ok(socket)
    })();
    match result {
        Ok(socket) => bridge(input.into_inner(), output.into_inner(), socket),
        Err(error) => {
            let _ = output.write_all(&frame(FAILED, error.to_string().as_bytes()));
            Err(error)
        }
    }
}

// A bounded byte bridge. Owner EOF ends it even when SSH stdin never closes.
fn bridge(mut input: File, mut output: File, mut socket: UnixStream) -> io::Result<()> {
    for fd in [input.as_raw_fd(), output.as_raw_fd(), socket.as_raw_fd()] {
        nonblocking(fd)?;
    }
    let mut up = VecDeque::new();
    let mut down = VecDeque::new();
    let mut ended = false;
    loop {
        if ended && down.is_empty() {
            return Ok(());
        }
        let events = poll(
            &[
                (
                    if up.len() < 65536 && !ended {
                        input.as_raw_fd()
                    } else {
                        -1
                    },
                    libc::POLLIN,
                ),
                (
                    if down.len() < 65536 && !ended {
                        socket.as_raw_fd()
                    } else {
                        -1
                    },
                    libc::POLLIN,
                ),
                (
                    if up.is_empty() || ended {
                        -1
                    } else {
                        socket.as_raw_fd()
                    },
                    libc::POLLOUT,
                ),
                (
                    if down.is_empty() {
                        -1
                    } else {
                        output.as_raw_fd()
                    },
                    libc::POLLOUT,
                ),
                (input.as_raw_fd(), 0),
            ],
            None,
        )
        .map_err(io::Error::other)?;
        if events[4] != 0 && events[0] == 0 {
            return Ok(());
        }
        for (index, event) in events.iter().take(2).enumerate() {
            if *event == 0 {
                continue;
            }
            let mut bytes = [0; 16384];
            let read = if index == 0 {
                input.read(&mut bytes)
            } else {
                socket.read(&mut bytes)
            };
            match read {
                Ok(0) if index == 0 => return Ok(()),
                Ok(0) => ended = true,
                Ok(count) => {
                    if index == 0 {
                        up.extend(&bytes[..count]);
                    } else {
                        down.extend(&bytes[..count]);
                    }
                }
                Err(error) if would_block(&error) => {}
                Err(error) => return Err(error),
            }
        }
        if events[2] != 0 {
            write(&mut socket, &mut up)?;
        }
        if events[3] != 0 {
            write(&mut output, &mut down)?;
        }
    }
}

pub(super) fn run() -> io::Result<()> {
    let mut input = Io::new(
        duplicate(0).map_err(io::Error::other)?,
        None,
        Some(Instant::now() + crate::ssh::SETUP_TIMEOUT),
    )
    .map_err(invalid)?;
    let (tag, bytes) = read(&mut input)?;
    if tag != HELLO {
        return Err(invalid("expected SSH owner initialization"));
    }
    let request: Request = serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    request
        .hello
        .check(&request.hello.operation)
        .map_err(invalid)?;
    if !request.create || request.epoch != 1 {
        return Err(invalid("SSH owner requires initial creation"));
    }
    let secret = request
        .secret
        .ok_or_else(|| invalid("missing SSH private capability"))?;
    let mut engine = Engine::new(true, request.hello.lease_ms);
    let path = directory(&request);
    fs::DirBuilder::new().mode(0o700).create(&path)?;
    let _directory = Directory(path.clone());
    let listener = UnixListener::bind(path.join("socket"))?;
    listener.set_nonblocking(true)?;
    duplicate(1).map_err(io::Error::other)?.write_all(b"1")?;
    drop(input);
    // Do not retain a disposable SSH process's standard streams. Diagnostic
    // output from the actual helper is separately sequenced and replayed.
    let null = File::options().read(true).write(true).open("/dev/null")?;
    for fd in [0, 1, 2] {
        if unsafe { libc::dup2(null.as_raw_fd(), fd) } < 0 {
            return Err(io::Error::last_os_error());
        }
    }
    drop(null);
    let (mut notified, notification) = io::pipe()?;
    nonblocking(notified.as_raw_fd())?;
    let (results, received) = mpsc::sync_channel(4);
    let mut candidates = 0;
    let mut epoch = 0;
    let mut helper: Option<Child> = None;
    let mut child_exit = None;
    let (mut exited, exit_notification) = io::pipe()?;
    let mut exit_noticed = false;
    let result = (|| {
        loop {
            if engine.tick()? {
                return Ok(());
            }
            if engine.activated && helper.is_none() && engine.retirement.is_none() {
                let mut command = Command::new(std::env::current_exe()?);
                command
                    .arg(&request.hello.operation)
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::piped());
                crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)
                    .map_err(invalid)?;
                let mut child = command.spawn()?;
                let mut notify = exit_notification.try_clone()?;
                child_exit = Some(
                    crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
                        let _ = notify.write_all(&[1]);
                    })
                    .map_err(io::Error::other)?,
                );
                engine.source = Some(File::from(OwnedFd::from(
                    child.stdout.take().expect("owner stdout"),
                )));
                engine.diagnostics = Some(File::from(OwnedFd::from(
                    child.stderr.take().expect("owner stderr"),
                )));
                engine.destination = Some(Destination::Remote(File::from(OwnedFd::from(
                    child.stdin.take().expect("owner stdin"),
                ))));
                for fd in [
                    engine.source.as_ref().expect("stdout").as_raw_fd(),
                    engine.diagnostics.as_ref().expect("stderr").as_raw_fd(),
                    engine.destination.as_ref().expect("stdin").as_raw_fd(),
                ] {
                    nonblocking(fd)?;
                }
                helper = Some(child);
            }
            let mut descriptors = engine.descriptors();
            descriptors.extend([
                (listener.as_raw_fd(), libc::POLLIN),
                (notified.as_raw_fd(), libc::POLLIN),
                (
                    if exit_noticed { -1 } else { exited.as_raw_fd() },
                    libc::POLLIN,
                ),
            ]);
            let wake = engine.wake();
            let events = match poll(&descriptors, Some(wake)) {
                Ok(events) => events,
                Err(_) if Instant::now() >= wake => continue,
                Err(error) => return Err(io::Error::other(error)),
            };
            engine.process(&events)?;
            if events[9] != 0 {
                exited.read_exact(&mut [0])?;
                engine.helper_exited = true;
                exit_noticed = true;
            }
            if events[7] != 0 {
                match listener.accept() {
                    Ok((socket, _)) if candidates < 4 && engine.retirement.is_none() => {
                        candidates += 1;
                        let hello = request.hello.clone();
                        let results = results.clone();
                        let mut notification = notification.try_clone()?;
                        let deadline = engine.deadline.min(Instant::now() + Duration::from_secs(2));
                        thread::spawn(move || {
                            let result = attachment::accept(socket, &hello, &secret, deadline);
                            let _ = results.send(result);
                            let _ = notification.write_all(&[1]);
                        });
                    }
                    Ok(_) => {}
                    Err(error) if would_block(&error) => {}
                    Err(error) => return Err(error),
                }
            }
            if events[8] != 0 {
                notified.read_exact(&mut [0])?;
                candidates -= 1;
                if let Ok((request, wire)) = received
                    .recv()
                    .map_err(|_| invalid("SSH attachment result lost"))?
                    && request.epoch > epoch
                    && Instant::now() < engine.deadline
                    && engine.retirement.is_none()
                {
                    epoch = request.epoch;
                    engine.install(
                        Link {
                            wire,
                            child: None,
                            proof_time: Instant::now(),
                        },
                        None,
                        Instant::now(),
                    )?;
                }
            }
        }
    })();
    // Expiry is terminal. Close helper input to invoke the existing cleanup
    // owners. Even an elapsed retirement wait is never a cleanup receipt.
    engine.destination.take();
    drop(listener);
    if let Err(error) = &result
        && let Some(link) = &mut engine.link
    {
        let deadline = Instant::now() + Duration::from_millis(100);
        let _ = link.wire.queue(FAILED, error.to_string().as_bytes());
        while !link.wire.empty() && Instant::now() < deadline {
            if poll(
                &[(link.wire.output.as_raw_fd(), libc::POLLOUT)],
                Some(deadline),
            )
            .is_err()
                || link.wire.write().is_err()
            {
                break;
            }
        }
    }
    engine.link.take();
    engine.source.take();
    engine.diagnostics.take();
    if let Some(mut child) = helper {
        let wait = engine
            .retirement
            .unwrap_or_else(|| Instant::now() + RETIREMENT)
            .saturating_duration_since(Instant::now());
        if !child_exit
            .as_mut()
            .expect("helper exit observer")
            .wait(wait)
            .map_err(io::Error::other)?
        {
            let _ = child.kill();
        }
        let _ = child.wait();
    }
    result
}
