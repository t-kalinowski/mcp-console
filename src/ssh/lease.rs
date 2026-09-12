//! A bounded control envelope shared by the two private SSH channels. The
//! application stream never contains lease messages. Its existing owner still
//! performs launcher/resolver retirement and produces the authoritative receipt.

use std::fs::File;
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

use super::launch_io::{Io, duplicate};
mod stream;

const VERSION: u32 = 1;
const LIMIT: usize = 32 * 1024;
const BLOCK: usize = 16 * 1024;
const WINDOW: usize = 256 * 1024;
const HELLO: u8 = 1;
const DATA: u8 = 2;
const ACK: u8 = 3;
const PING: u8 = 4;
const PONG: u8 = 5;
const END: u8 = 6;
const ENDED: u8 = 7;

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Hello {
    version: u32,
    build: String,
    operation: String,
    lease_ms: u64,
}

impl Hello {
    fn check(&self, operation: &str) -> Result<(), String> {
        if self.version != VERSION
            || self.build != env!("CARGO_PKG_VERSION")
            || self.operation != operation
        {
            return Err("incompatible SSH lease protocol, channel, or Console build".into());
        }
        crate::settings::validate_ssh_lease(self.lease_ms)
    }
}

fn challenge() -> Result<Vec<u8>, String> {
    let mut bytes = vec![0; 32];
    File::open("/dev/urandom")
        .and_then(|mut random| random.read_exact(&mut bytes))
        .map_err(|e| e.to_string())?;
    Ok(bytes)
}

fn frame(tag: u8, body: &[u8]) -> Vec<u8> {
    assert!(body.len() <= LIMIT);
    let mut bytes = Vec::with_capacity(5 + body.len());
    bytes.push(tag);
    bytes.extend((body.len() as u32).to_be_bytes());
    bytes.extend(body);
    bytes
}

fn read(reader: &mut impl Read) -> Result<(u8, Vec<u8>), String> {
    let mut tag = [0];
    reader
        .read_exact(&mut tag)
        .map_err(|e| format!("SSH lease stream ended: {e}"))?;
    let body = super::read_payload(reader, LIMIT).map_err(|e| e.to_string())?;
    Ok((tag[0], body))
}

pub(crate) fn run(remote: bool, operation: &str) -> Result<(), String> {
    if !matches!(operation, "ssh-launch" | "ssh-prepare") {
        return Err("unknown SSH ownership channel".into());
    }
    if remote {
        owner(operation)
    } else {
        controller(operation)
    }
}

fn controller(operation: &str) -> Result<(), String> {
    let target = std::env::var("MCP_CONSOLE_SSH_TARGET").map_err(|e| e.to_string())?;
    // This entry point is single-threaded. Do not pass controller configuration
    // into OpenSSH's environment or any workload.
    unsafe { std::env::remove_var("MCP_CONSOLE_SSH_TARGET") };
    let target: crate::settings::SshTarget =
        serde_json::from_str(&target).map_err(|e| e.to_string())?;
    let hello = Hello {
        version: VERSION,
        build: env!("CARGO_PKG_VERSION").into(),
        operation: operation.into(),
        lease_ms: target.lease_ms,
    };
    hello.check(operation)?;
    let session = super::Session::new(target, Vec::new());
    let mut command = session.ssh_command(operation)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command
        .spawn()
        .map_err(|e| format!("cannot start SSH: {e}"))?;
    let mut exit = crate::process_exit::ChildExitWaiter::start(child.id())?;
    let mut retirement = None;
    let result = (|| {
        let deadline = Instant::now() + super::SETUP_TIMEOUT;
        let mut input = Io::new(
            child.stdout.take().expect("SSH stdout"),
            None,
            Some(deadline),
        )?;
        let mut output = Io::new(child.stdin.take().expect("SSH stdin"), None, Some(deadline))?;
        output
            .write_all(&frame(
                HELLO,
                &serde_json::to_vec(&hello).map_err(|e| e.to_string())?,
            ))
            .map_err(|e| e.to_string())?;
        let (tag, bytes) = read(&mut input)?;
        if tag != HELLO {
            return Err("expected SSH lease compatibility reply".into());
        }
        let reply: Hello = serde_json::from_slice(&bytes).map_err(|e| e.to_string())?;
        reply.check(operation)?;
        if reply.lease_ms != hello.lease_ms {
            return Err("SSH lease changed during setup".into());
        }
        let (tag, token) = read(&mut input)?;
        if tag != PING || token.len() != 32 {
            return Err("expected SSH lease challenge".into());
        }
        output
            .write_all(&frame(PONG, &token))
            .map_err(|e| e.to_string())?;
        stream::pump(
            false,
            hello.lease_ms,
            input.into_inner(),
            output.into_inner(),
            duplicate(0)?,
            duplicate(1)?,
            &mut retirement,
        )
    })();
    let wait = retirement.map_or(Duration::from_secs(6), |deadline| {
        deadline
            .saturating_duration_since(Instant::now())
            .min(Duration::from_secs(6))
    });
    if result.is_err() || !exit.wait(wait)? {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    result?;
    if !status.success() {
        return Err(format!("SSH exited with {status}"));
    }
    Ok(())
}

fn owner(operation: &str) -> Result<(), String> {
    let deadline = Instant::now() + super::SETUP_TIMEOUT;
    let mut input = Io::new(duplicate(0)?, None, Some(deadline))?;
    let mut output = Io::new(duplicate(1)?, None, Some(deadline))?;
    let (tag, bytes) = read(&mut input)?;
    if tag != HELLO {
        return Err("expected SSH lease bootstrap".into());
    }
    let hello: Hello =
        serde_json::from_slice(&bytes).map_err(|e| format!("invalid SSH lease bootstrap: {e}"))?;
    hello.check(operation)?;
    output
        .write_all(&frame(HELLO, &bytes))
        .map_err(|e| e.to_string())?;
    let token = challenge()?;
    output
        .write_all(&frame(PING, &token))
        .map_err(|e| e.to_string())?;
    let mut input = Io::new(
        input.into_inner(),
        None,
        Some(Instant::now() + Duration::from_millis(hello.lease_ms)),
    )?;
    if read(&mut input)? != (PONG, token) {
        return Err("invalid SSH lease challenge response".into());
    }
    // No discovery, preflight, or resolver execution precedes the round trip.
    let mut command = Command::new(std::env::current_exe().map_err(|e| e.to_string())?);
    command
        .arg(operation)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)?;
    let mut child = command.spawn().map_err(|e| e.to_string())?;
    let mut exit = crate::process_exit::ChildExitWaiter::start(child.id())?;
    let mut retirement = None;
    let result = stream::pump(
        true,
        hello.lease_ms,
        input.into_inner(),
        output.into_inner(),
        child.stdout.take().expect("owner stdout"),
        child.stdin.take().expect("owner stdin"),
        &mut retirement,
    );
    // EOF requests retirement through the existing launch/preparation owner.
    // The lease bounds this request, never proves that native cleanup succeeded.
    let wait = retirement.map_or(Duration::from_secs(8), |deadline| {
        deadline.saturating_duration_since(Instant::now())
    });
    if !exit.wait(wait)? {
        let _ = child.kill();
    }
    let status = child.wait().map_err(|e| e.to_string())?;
    result?;
    if !status.success() {
        return Err(format!("remote ownership helper exited with {status}"));
    }
    Ok(())
}
