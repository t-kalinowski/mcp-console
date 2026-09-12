//! Mutual attachment authentication and stream fencing. The capability only
//! crosses the original encrypted SSH bootstrap and an owner startup pipe.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::process::{Child, Stdio};
use std::time::Instant;

use hmac::{Hmac, KeyInit, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

use super::{FAILED, HELLO, Hello, LIMIT, PROOF, Secret, challenge, frame, invalid, json, read};
use crate::ssh::launch_io::{Io, poll};

#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub hello: Hello,
    pub epoch: u64,
    pub nonce: Secret,
    pub create: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub secret: Option<Secret>,
}

impl Request {
    fn binding(&self) -> Vec<u8> {
        let mut public = self.clone();
        public.secret = None;
        json(&public)
    }
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Challenge {
    nonce: Secret,
    mac: Secret,
}

fn mac(secret: &Secret, parts: &[&[u8]]) -> Hmac<Sha256> {
    let mut value = Hmac::<Sha256>::new_from_slice(secret).expect("32-byte HMAC key");
    for part in parts {
        value.update(part);
    }
    value
}

fn signed(secret: &Secret, parts: &[&[u8]]) -> Secret {
    mac(secret, parts).finalize().into_bytes().into()
}

pub(super) fn accept(
    socket: UnixStream,
    hello: &Hello,
    secret: &Secret,
    deadline: Instant,
) -> io::Result<(Request, Wire)> {
    let mut input = Io::new(socket.try_clone()?, None, Some(deadline)).map_err(invalid)?;
    let mut output = Io::new(socket, None, Some(deadline)).map_err(invalid)?;
    let (tag, bytes) = read(&mut input)?;
    if tag != HELLO {
        return Err(invalid("expected SSH attachment request"));
    }
    let request: Request = serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    if &request.hello != hello || request.secret.is_some() || request.epoch == 0 {
        return Err(invalid(
            "SSH ownership identity, generation, or compatibility mismatch",
        ));
    }
    let binding = request.binding();
    let nonce = challenge()?;
    output.write_all(&frame(
        HELLO,
        &json(&Challenge {
            nonce,
            mac: signed(secret, &[b"owner", &binding, &nonce]),
        }),
    ))?;
    let (tag, proof) = read(&mut input)?;
    if tag != PROOF
        || mac(secret, &[b"controller", &binding, &nonce])
            .verify_slice(&proof)
            .is_err()
    {
        return Err(invalid("SSH attachment authentication failed"));
    }
    let key = signed(secret, &[b"attachment", &binding, &nonce]);
    let wire = Wire::new(
        File::from(OwnedFd::from(input.into_inner())),
        File::from(OwnedFd::from(output.into_inner())),
        key,
        request.epoch,
        true,
    )?;
    Ok((request, wire))
}

pub(super) struct Link {
    pub wire: Wire,
    pub child: Option<Child>,
    pub proof_time: Instant,
}

impl Drop for Link {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

pub(super) fn connect(
    session: &crate::ssh::Session,
    request: &Request,
    secret: &Secret,
    deadline: Instant,
    cancellation: std::io::PipeReader,
) -> io::Result<Link> {
    let mut command = session
        .ssh_command(&request.hello.operation)
        .map_err(invalid)?;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    crate::process_descriptors::close_unlisted_from_multithreaded_parent(&mut command)
        .map_err(invalid)?;
    let cancelled = cancellation.try_clone()?;
    let mut child = command.spawn()?;
    let mut proof_time = Instant::now();
    let result = (|| {
        let mut input = Io::new(
            child.stdout.take().expect("SSH stdout"),
            Some(cancellation.try_clone()?),
            Some(deadline),
        )
        .map_err(invalid)?;
        let mut output = Io::new(
            child.stdin.take().expect("SSH stdin"),
            Some(cancellation),
            Some(deadline),
        )
        .map_err(invalid)?;
        output.write_all(&frame(HELLO, &json(request)))?;
        let (tag, bytes) = read(&mut input)?;
        if tag == FAILED {
            return Err(invalid(String::from_utf8_lossy(&bytes)));
        }
        if tag != HELLO {
            return Err(invalid("expected SSH authenticated owner reply"));
        }
        let reply: Challenge =
            serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
        let binding = request.binding();
        if mac(secret, &[b"owner", &binding, &reply.nonce])
            .verify_slice(&reply.mac)
            .is_err()
        {
            return Err(invalid("SSH owner authentication failed"));
        }
        proof_time = Instant::now();
        output.write_all(&frame(
            PROOF,
            &signed(secret, &[b"controller", &binding, &reply.nonce]),
        ))?;
        let key = signed(secret, &[b"attachment", &binding, &reply.nonce]);
        Wire::new(
            File::from(OwnedFd::from(input.into_inner())),
            File::from(OwnedFd::from(output.into_inner())),
            key,
            request.epoch,
            false,
        )
    })();
    match result {
        Ok(wire) => Ok(Link {
            wire,
            child: Some(child),
            proof_time,
        }),
        Err(error) => {
            if error.kind() != io::ErrorKind::InvalidData {
                // EOF may precede the SSH exit notification. Observe its
                // status without racing a kill against a definite command
                // failure; cancellation and the attempt deadline still apply.
                let (exited, notification) = io::pipe()?;
                let _exit =
                    crate::process_exit::ChildExitWaiter::start_notifying(child.id(), move || {
                        drop(notification)
                    })
                    .map_err(io::Error::other)?;
                let _ = poll(
                    &[
                        (exited.as_raw_fd(), libc::POLLIN),
                        (cancelled.as_raw_fd(), libc::POLLIN),
                    ],
                    Some(deadline),
                );
            }
            let _ = child.kill();
            let status = child.wait()?;
            if error.kind() != io::ErrorKind::InvalidData
                && status.code().is_some_and(|code| code != 0 && code != 255)
            {
                return Err(invalid(format!(
                    "SSH bootstrap command exited with {status}; remote ownership is unconfirmed"
                )));
            }
            Err(error)
        }
    }
}

pub(super) struct Wire {
    pub input: File,
    pub output: File,
    pub epoch: u64,
    pub ready: bool,
    remote: bool,
    key: Secret,
    sent: u64,
    received: u64,
    incoming: Vec<u8>,
    encoded: VecDeque<u8>,
}

impl Wire {
    fn new(input: File, output: File, key: Secret, epoch: u64, remote: bool) -> io::Result<Self> {
        super::stream::nonblocking(input.as_raw_fd())?;
        super::stream::nonblocking(output.as_raw_fd())?;
        Ok(Self {
            input,
            output,
            key,
            epoch,
            remote,
            ready: false,
            sent: 0,
            received: 0,
            incoming: Vec::new(),
            encoded: VecDeque::new(),
        })
    }
    pub fn empty(&self) -> bool {
        self.encoded.is_empty()
    }
    pub fn queue(&mut self, tag: u8, body: &[u8]) -> io::Result<()> {
        if body.len() + 40 > LIMIT || self.encoded.len() + body.len() + 45 > 2 * LIMIT {
            return Err(invalid("SSH control output capacity exhausted"));
        }
        self.sent += 1;
        let mut payload = self.sent.to_be_bytes().to_vec();
        payload.extend(body);
        let signature = signed(
            &self.key,
            &[
                &[u8::from(self.remote), tag],
                &self.epoch.to_be_bytes(),
                &payload,
            ],
        );
        payload.extend(signature);
        self.encoded.extend(frame(tag, &payload));
        Ok(())
    }
    pub fn write(&mut self) -> io::Result<()> {
        super::stream::write(&mut self.output, &mut self.encoded)
    }
    pub fn read(&mut self) -> io::Result<Option<(u8, Vec<u8>)>> {
        let needed = if self.incoming.len() < 5 {
            5 - self.incoming.len()
        } else {
            let length =
                u32::from_be_bytes(self.incoming[1..5].try_into().expect("length")) as usize;
            if !(40..=LIMIT).contains(&length) {
                return Err(invalid("invalid SSH authenticated frame length"));
            }
            5 + length - self.incoming.len()
        };
        if needed > 0 {
            let mut bytes = [0; LIMIT + 5];
            match self.input.read(&mut bytes[..needed]) {
                Ok(0) => return Err(io::ErrorKind::UnexpectedEof.into()),
                Ok(count) => self.incoming.extend(&bytes[..count]),
                Err(error) if super::stream::would_block(&error) => return Ok(None),
                Err(error) => return Err(error),
            }
        }
        if self.incoming.len() < 5 {
            return Ok(None);
        }
        let length = u32::from_be_bytes(self.incoming[1..5].try_into().expect("length")) as usize;
        if !(40..=LIMIT).contains(&length) {
            return Err(invalid("invalid SSH authenticated frame length"));
        }
        if self.incoming.len() < length + 5 {
            return Ok(None);
        }
        let tag = self.incoming[0];
        let body = &self.incoming[5..5 + length - 32];
        let signature = &self.incoming[5 + length - 32..];
        let id = u64::from_be_bytes(body[..8].try_into().expect("frame serial"));
        if id != self.received + 1
            || mac(
                &self.key,
                &[
                    &[u8::from(!self.remote), tag],
                    &self.epoch.to_be_bytes(),
                    body,
                ],
            )
            .verify_slice(signature)
            .is_err()
        {
            return Err(invalid(
                "SSH attachment epoch, serial, or authentication failed",
            ));
        }
        self.received = id;
        let bytes = body[8..].to_vec();
        self.incoming.clear();
        Ok(Some((tag, bytes)))
    }
}

pub(super) fn wait_ready(
    link: &mut Link,
    deadline: Instant,
    cancelled: &std::io::PipeReader,
) -> io::Result<(u8, Vec<u8>)> {
    loop {
        let ready = poll(
            &[
                (link.wire.input.as_raw_fd(), libc::POLLIN),
                (cancelled.as_raw_fd(), libc::POLLIN),
            ],
            Some(deadline),
        )
        .map_err(io::Error::other)?;
        if ready[1] != 0 {
            return Err(io::Error::other("SSH recovery cancelled"));
        }
        if let Some(frame) = link.wire.read()? {
            return Ok(frame);
        }
    }
}
