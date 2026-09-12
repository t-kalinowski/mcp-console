//! Finite, controller-owned SSH sessions. Attachments carry authenticated,
//! sequenced streams; existing launch/preparation helpers own workload cleanup.

use std::fs::File;
use std::io::{self, Read};
use std::time::Duration;

use serde::{Deserialize, Serialize};

mod attachment;
mod controller;
mod flow;
mod owner;
pub(crate) mod status;
mod stream;

const VERSION: u32 = 2;
const LIMIT: usize = 32 * 1024;
const BLOCK: usize = 16 * 1024;
const WINDOW: usize = 256 * 1024;
const PACKETS: usize = 128;
const HELLO: u8 = 1;
const DATA: u8 = 2;
const ACK: u8 = 3;
const PING: u8 = 4;
const PONG: u8 = 5;
const END: u8 = 6;
const ENDED: u8 = 7;
const PROOF: u8 = 8;
const READY: u8 = 9;
const FAILED: u8 = 10;
const RETIREMENT: Duration = Duration::from_secs(8);

type Secret = [u8; 32];

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
struct Hello {
    version: u32,
    build: String,
    operation: String,
    lease_ms: u64,
    owner: String,
    generation: u64,
}

impl Hello {
    fn check(&self, operation: &str) -> Result<(), String> {
        if self.version != VERSION
            || self.build != env!("CARGO_PKG_VERSION")
            || self.operation != operation
        {
            return Err("incompatible SSH recovery protocol, channel, or Console build".into());
        }
        if self.owner.len() != 32 || !self.owner.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("invalid SSH ownership identity".into());
        }
        crate::settings::validate_ssh_lease(self.lease_ms)
    }
}

fn challenge() -> io::Result<Secret> {
    let mut bytes = [0; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
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

fn read(reader: &mut impl Read) -> io::Result<(u8, Vec<u8>)> {
    let mut header = [0; 5];
    reader.read_exact(&mut header)?;
    let length = u32::from_be_bytes(header[1..].try_into().expect("frame length")) as usize;
    if length > LIMIT {
        return Err(invalid(format!("SSH frame exceeds {LIMIT} bytes")));
    }
    let mut body = vec![0; length];
    reader.read_exact(&mut body)?;
    Ok((header[0], body))
}

fn json<T: Serialize>(value: &T) -> Vec<u8> {
    serde_json::to_vec(value).expect("SSH protocol value is serializable")
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.into())
}

pub(crate) fn run(remote: bool, operation: &str) -> Result<(), String> {
    if !matches!(operation, "ssh-launch" | "ssh-prepare") {
        return Err("unknown SSH ownership channel".into());
    }
    if remote {
        owner::tunnel(operation)
    } else {
        controller::run(operation)
    }
    .map_err(|e| e.to_string())
}

pub(crate) fn run_owner() -> Result<(), String> {
    owner::run().map_err(|e| e.to_string())
}
