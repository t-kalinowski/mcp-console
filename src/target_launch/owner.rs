//! Shared controller-loss observation and non-TTY target byte transport.
//! Concrete adapters own creation, identity, policy, and retirement receipts.
use super::process::Cancel;
use super::transfer::{Io, duplicate, poll};
use crate::target_launch::{self, Bootstrap, Hello, Retired};
use std::io::{self, BufRead, Read, Write};
use std::os::fd::AsRawFd;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

/// The same bounded controller-to-owner envelope carries either captured provider.
#[derive(serde::Deserialize, serde::Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Request<T> {
    pub session: T,
    pub name: String,
    pub probe: bool,
    pub bootstrap: Bootstrap,
}

static SIGNAL: AtomicI32 = AtomicI32::new(-1);

pub(crate) fn token() -> Result<String, String> {
    let mut bytes = [0; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|e| e.to_string())?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

extern "C" fn stop_signal(_: libc::c_int) {
    let fd = SIGNAL.load(Ordering::Relaxed);
    if fd >= 0 {
        unsafe {
            libc::write(fd, b"1".as_ptr().cast(), 1);
        }
    }
}

pub(crate) fn run(
    protocol: target_launch::Protocol,
    operation: impl FnOnce(&Cancel) -> Retired,
) -> Result<(), String> {
    let cancel = Cancel::new(protocol)?;
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
    let retired = operation(&cancel);
    let confirmed = retired.confirmed;
    let error = retired.error.clone();
    let retired = serde_json::to_vec(&retired).map_err(|e| e.to_string())?;
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
    if confirmed {
        delivered
    } else {
        Err(error.unwrap_or_else(|| "target retirement is unconfirmed".into()))
    }
}

pub(crate) fn outcome(result: Result<(), String>, cleanup: Result<(), String>) -> Retired {
    let confirmed = cleanup.is_ok();
    let result = match (result, cleanup) {
        (Ok(()), result) | (result, Ok(())) => result,
        (Err(error), Err(cleanup)) => Err(format!("{error}; {cleanup}")),
    };
    Retired {
        confirmed,
        error: result.err(),
    }
}

pub(crate) fn attach(
    mut command: Command,
    bootstrap: &Bootstrap,
    hello: Hello,
    protocol: target_launch::Protocol,
    cancel: &Cancel,
) -> Result<(), String> {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .process_group(0);
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("cannot execute target transport: {e}"))?;
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
    let output_task = std::thread::spawn(move || -> Result<(), String> {
        let _done = output_done;
        let mut source =
            io::BufReader::new(crate::process_output::RelayOutput::new(stdout, output_exit));
        // sbx can put OCI setup errors on stdout. Reject them as protocol data,
        // retaining the available bounded prefix as a controller diagnostic.
        let prefix = source.fill_buf().map_err(|error| error.to_string())?;
        if prefix
            .first()
            .is_some_and(|tag| !matches!(*tag, target_launch::HELLO | target_launch::RETIRED))
        {
            return Err(format!(
                "unexpected stdout in {} launch protocol: {}",
                protocol.0,
                String::from_utf8_lossy(prefix)
            ));
        }
        let retirement = target_launch::Retirement::default();
        let mut source = target_launch::Output::new(source, protocol, retirement);
        let mut destination = Io::new(duplicate(1)?, Some(output_cancel), None)?;
        let hello = serde_json::to_vec(&hello).map_err(|e| e.to_string())?;
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
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
    } else if child.wait().map_err(|e| e.to_string())?.success() {
        // Preserve queued output after an ordinary exit. A failed attachment
        // must instead cancel blocked forwarding so container retirement can
        // proceed even when the controller is not draining this pipe.
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
        unsafe {
            libc::kill(-(child.id() as i32), libc::SIGKILL);
        }
    }
    if !exit.wait(Duration::from_secs(1))? {
        return Err("target execution transport did not exit".into());
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = input_task.join();
    let output = output_task
        .join()
        .map_err(|_| "target output task panicked")?;
    output?;
    if !status.success() {
        return Err(format!("target execution transport exited with {status}"));
    }
    Ok(())
}
