//! One readiness loop services the application, replay window, and lease.
//! A missing attachment or a full output pipe cannot suspend its deadline.

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};

use super::attachment::Link;
use super::flow::{Cursor, Flow};
use super::status;
use super::{
    ACK, BLOCK, DATA, END, ENDED, FAILED, PING, PONG, READY, RETIREMENT, Secret, challenge,
    invalid, json,
};

pub(super) fn nonblocking(fd: i32) -> io::Result<()> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
pub(super) fn would_block(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::Interrupted
    )
}
pub(super) fn write(writer: &mut impl Write, bytes: &mut VecDeque<u8>) -> io::Result<()> {
    if bytes.is_empty() {
        return Ok(());
    }
    match writer.write(bytes.as_slices().0) {
        Ok(0) => Err(io::ErrorKind::WriteZero.into()),
        Ok(count) => {
            bytes.drain(..count);
            Ok(())
        }
        Err(error) if would_block(&error) => Ok(()),
        Err(error) => Err(error),
    }
}

pub(super) enum Destination {
    Remote(File),
    Local(status::Writer),
}
impl AsRawFd for Destination {
    fn as_raw_fd(&self) -> i32 {
        match self {
            Self::Remote(file) => file.as_raw_fd(),
            Self::Local(writer) => writer.as_raw_fd(),
        }
    }
}
impl Write for Destination {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        match self {
            Self::Remote(file) => file.write(bytes),
            Self::Local(writer) => writer.write(bytes),
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(super) struct Engine {
    pub link: Option<Link>,
    pub source: Option<File>,
    pub diagnostics: Option<File>,
    pub destination: Option<Destination>,
    pub stderr: Option<File>,
    pub source_closed: bool,
    pub activated: bool,
    pub helper_exited: bool,
    pub output_abandoned: bool,
    input_closed: bool,
    pub retirement: Option<Instant>,
    pub deadline: Instant,
    pub flow: Flow,
    pub remote: bool,
    lease: Duration,
    heartbeat: Duration,
    next_ping: Instant,
    pending_ping: Option<Secret>,
    last_response: Instant,
    last_challenge: Instant,
    advertised: Cursor,
    peer_closed: bool,
    peer_end_seen: bool,
    end_sent: bool,
    ended_sent: bool,
    finished: bool,
}

impl Engine {
    pub fn new(remote: bool, lease_ms: u64) -> Self {
        let now = Instant::now();
        let lease = Duration::from_millis(lease_ms);
        Self {
            link: None,
            source: None,
            diagnostics: None,
            destination: None,
            stderr: None,
            source_closed: false,
            activated: false,
            helper_exited: false,
            output_abandoned: false,
            input_closed: false,
            retirement: None,
            deadline: now
                + if remote {
                    lease
                } else {
                    crate::ssh::SETUP_TIMEOUT
                },
            flow: Flow::default(),
            remote,
            lease,
            heartbeat: lease / 6,
            next_ping: now,
            pending_ping: None,
            last_response: now,
            last_challenge: now,
            advertised: Cursor::default(),
            peer_closed: false,
            peer_end_seen: false,
            end_sent: false,
            ended_sent: false,
            finished: false,
        }
    }
    pub fn status(&mut self, state: &'static str) {
        if let Some(Destination::Local(writer)) = &mut self.destination {
            writer.status(state);
        }
    }
    pub fn install(
        &mut self,
        mut link: Link,
        cursor: Option<Cursor>,
        started: Instant,
    ) -> io::Result<()> {
        if self.retirement.is_some() || Instant::now() >= self.deadline {
            return Err(invalid(
                "SSH ownership expired or retirement already requested",
            ));
        }
        if !self.remote && !self.activated {
            self.deadline = started + self.lease;
            self.activated = true;
            self.last_response = started;
        }
        if let Some(cursor) = cursor {
            self.flow.resume(cursor)?;
            link.wire.ready = true;
        }
        link.wire.queue(READY, &json(&self.flow.delivered))?;
        self.link = Some(link); // Dropping the old stream fences its epoch.
        self.pending_ping = None;
        self.next_ping = Instant::now();
        self.last_challenge = Instant::now();
        self.advertised = self.flow.delivered;
        self.end_sent = false;
        self.peer_end_seen = false;
        self.ended_sent = false;
        self.status("connected");
        Ok(())
    }
    pub fn lost(&mut self) {
        self.link.take();
        self.pending_ping = None;
        self.status("recovering");
    }
    fn application_closed(&mut self) {
        self.destination.take();
        self.flow.discard_input(); // Unwritten bytes are never acknowledged.
        if self.remote {
            // The helper can close stdin before its queued terminal receipt is
            // drained. Late generation controls must not destroy that receipt.
            self.input_closed = true;
        } else {
            // The server cancelled its reader. Request retirement, then leave
            // without representing discarded output as ingested or confirmed.
            self.output_abandoned = true;
            self.local_closed();
        }
    }
    pub fn local_closed(&mut self) {
        self.source_closed = true;
        self.source.take();
        self.retirement.get_or_insert(Instant::now() + RETIREMENT);
        self.status("retiring");
    }
    pub fn tick(&mut self) -> io::Result<bool> {
        if self.activated
            && self.source.is_none()
            && self.diagnostics.is_none()
            && (!self.remote || self.helper_exited)
        {
            self.source_closed = true;
            if !self.remote {
                self.local_closed();
            }
        }
        let now = Instant::now();
        if self.retirement.is_some_and(|deadline| now >= deadline) {
            return Err(io::Error::other(
                "SSH retirement deadline exceeded; cleanup is unconfirmed",
            ));
        }
        if now >= self.deadline {
            return Err(io::Error::other(
                "SSH controller lease expired; remote retirement is unconfirmed",
            ));
        }
        if !self.remote && self.link.is_some() && now >= self.last_challenge + self.heartbeat * 2 {
            self.lost();
        }
        let Some(link) = &mut self.link else {
            return Ok(false);
        };
        if link.wire.empty() && link.wire.ready {
            if self.remote && self.pending_ping.is_none() && now >= self.next_ping {
                let token = challenge()?;
                link.wire.queue(PING, &token)?;
                self.pending_ping = Some(token);
            } else if self.advertised != self.flow.delivered {
                link.wire.queue(ACK, &json(&self.flow.delivered))?;
                self.advertised = self.flow.delivered;
            } else if self.peer_end_seen
                && !self.output_abandoned
                && self.flow.destination().is_none()
                && !self.ended_sent
            {
                if self.remote {
                    self.destination.take();
                }
                link.wire.queue(ENDED, &json(&self.flow.delivered))?;
                self.ended_sent = true;
            } else if let Some(bytes) = self.flow.send() {
                link.wire.queue(DATA, &bytes)?;
            } else if self.source_closed && !self.end_sent && self.flow.transmitted() {
                link.wire.queue(END, &json(&self.flow.end()))?;
                self.end_sent = true;
            }
        }
        Ok(link.wire.empty()
            && if self.remote {
                self.finished
            } else {
                self.ended_sent || (self.output_abandoned && self.end_sent)
            })
    }
    pub fn wake(&self) -> Instant {
        let mut deadline = self
            .retirement
            .map_or(self.deadline, |time| time.min(self.deadline));
        if let Some(link) = &self.link {
            if !self.remote {
                deadline = deadline.min(self.last_challenge + self.heartbeat * 2);
            }
            if self.remote && link.wire.ready && link.wire.empty() && self.pending_ping.is_none() {
                deadline = deadline.min(self.next_ping);
            }
        }
        deadline
    }
    pub fn descriptors(&self) -> Vec<(i32, libc::c_short)> {
        let capacity = self.flow.capacity() > 0;
        let status =
            matches!(&self.destination, Some(Destination::Local(writer)) if writer.pending());
        let destination = match self.flow.destination() {
            Some(1) => self.stderr.as_ref().map_or(-1, AsRawFd::as_raw_fd),
            Some(0) => self.destination.as_ref().map_or(-1, AsRawFd::as_raw_fd),
            _ => -1,
        };
        vec![
            (
                self.link
                    .as_ref()
                    .map_or(-1, |link| link.wire.input.as_raw_fd()),
                libc::POLLIN,
            ),
            (
                self.link
                    .as_ref()
                    .filter(|link| !link.wire.empty())
                    .map_or(-1, |link| link.wire.output.as_raw_fd()),
                libc::POLLOUT,
            ),
            (
                self.source
                    .as_ref()
                    .filter(|_| {
                        capacity && !self.source_closed && (self.remote || !self.peer_closed)
                    })
                    .map_or(-1, AsRawFd::as_raw_fd),
                libc::POLLIN,
            ),
            (
                self.diagnostics
                    .as_ref()
                    .filter(|_| capacity)
                    .map_or(-1, AsRawFd::as_raw_fd),
                libc::POLLIN,
            ),
            (destination, libc::POLLOUT),
            (
                if self.remote {
                    -1
                } else {
                    self.source.as_ref().map_or(-1, AsRawFd::as_raw_fd)
                },
                0,
            ),
            (
                if status {
                    self.destination.as_ref().map_or(-1, AsRawFd::as_raw_fd)
                } else {
                    -1
                },
                libc::POLLOUT,
            ),
        ]
    }
    pub fn process(&mut self, events: &[libc::c_short]) -> io::Result<()> {
        if !self.remote && events[5] != 0 && events[2] == 0 {
            self.local_closed();
        }
        if events[6] != 0
            && let Some(Destination::Local(writer)) = &mut self.destination
            && let Err(error) = writer.service()
        {
            if error.kind() != io::ErrorKind::BrokenPipe {
                return Err(error);
            }
            self.application_closed();
        }
        if events[4] != 0 && !self.input_closed && !self.output_abandoned {
            match self.flow.destination() {
                Some(1) => self.flow.deliver(
                    self.stderr
                        .as_mut()
                        .ok_or_else(|| invalid("SSH diagnostic destination closed"))?,
                )?,
                Some(0) => {
                    let result = self.flow.deliver(
                        self.destination
                            .as_mut()
                            .ok_or_else(|| invalid("SSH application input closed"))?,
                    );
                    if let Err(error) = result {
                        if error.kind() != io::ErrorKind::BrokenPipe {
                            return Err(error);
                        }
                        self.application_closed();
                    }
                }
                _ => {}
            }
        }
        for (index, kind) in [(2, 0), (3, 1)] {
            if events[index] == 0 || self.flow.capacity() == 0 {
                continue;
            }
            let source = if kind == 0 {
                &mut self.source
            } else {
                &mut self.diagnostics
            };
            let Some(reader) = source.as_mut() else {
                continue;
            };
            let mut bytes = [0; BLOCK];
            match reader.read(&mut bytes[..self.flow.capacity()]) {
                Ok(0) => {
                    source.take();
                }
                Ok(count) => self.flow.push(kind, bytes[..count].to_vec()),
                Err(error) if would_block(&error) => {}
                Err(error) => return Err(error),
            }
        }
        let result = (|| {
            if events[1] != 0
                && let Some(link) = &mut self.link
            {
                link.wire.write()?;
            }
            if events[0] != 0
                && let Some(link) = &mut self.link
                && let Some((tag, body)) = link.wire.read()?
            {
                self.receive(tag, &body)?;
            }
            Ok::<(), io::Error>(())
        })();
        if let Err(error) = result {
            if error.kind() == io::ErrorKind::InvalidData {
                return Err(error);
            }
            self.lost();
        }
        Ok(())
    }
    fn receive(&mut self, tag: u8, bytes: &[u8]) -> io::Result<()> {
        if Instant::now() >= self.deadline {
            return Err(invalid("SSH owner lease has expired"));
        }
        let link = self.link.as_mut().expect("current attachment");
        let cursor = || serde_json::from_slice::<Cursor>(bytes).map_err(|e| invalid(e.to_string()));
        match tag {
            READY if self.remote && !link.wire.ready => {
                self.flow.resume(cursor()?)?;
                link.wire.ready = true;
            }
            DATA if link.wire.ready => {
                self.flow.receive(bytes, self.remote)?;
                if self.input_closed || self.output_abandoned {
                    self.flow.discard_input();
                }
            }
            ACK if link.wire.ready => self.flow.acknowledge(cursor()?)?,
            PING if !self.remote && link.wire.ready && bytes.len() == 32 => {
                link.wire.queue(PONG, bytes)?;
                self.deadline = self.last_response + self.lease;
                self.last_response = Instant::now();
                self.last_challenge = Instant::now();
            }
            PONG if self.remote
                && self.pending_ping.as_ref().map(|token| token.as_slice()) == Some(bytes) =>
            {
                self.pending_ping = None;
                self.deadline = Instant::now() + self.lease;
                self.next_ping = Instant::now() + self.heartbeat;
                if self.retirement.is_none() {
                    self.activated = true;
                }
            }
            END if link.wire.ready => {
                self.flow.accept_end(cursor()?)?;
                self.peer_closed = true;
                self.peer_end_seen = true;
                if self.remote {
                    self.retirement.get_or_insert(Instant::now() + RETIREMENT);
                    self.flow.discard_input();
                    self.destination.take();
                    if !self.activated {
                        self.source_closed = true;
                    }
                }
            }
            ENDED if self.end_sent => {
                if self.remote {
                    if cursor()? != self.flow.end() {
                        return Err(invalid("invalid SSH terminal acknowledgment"));
                    }
                    self.finished = true;
                } else {
                    self.flow.acknowledge(cursor()?)?;
                }
            }
            FAILED => {
                return Err(invalid(format!(
                    "remote SSH owner failed; retirement is unconfirmed: {}",
                    String::from_utf8_lossy(bytes)
                )));
            }
            _ => return Err(invalid("unexpected SSH recovery control frame")),
        }
        Ok(())
    }
}
