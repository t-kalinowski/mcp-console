//! One readiness loop owns framing, bounded flow control, and lease deadlines.
//! Workload backpressure disables data reads, never control reads or expiry.

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::os::fd::AsRawFd;
use std::time::{Duration, Instant};

use super::{ACK, BLOCK, DATA, END, ENDED, LIMIT, PING, PONG, WINDOW, challenge, frame};
use crate::ssh::launch_io::poll;

fn nonblocking(fd: i32) -> Result<(), String> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(std::io::Error::last_os_error().to_string());
    }
    Ok(())
}

fn write(writer: &mut impl Write, bytes: &mut VecDeque<u8>) -> Result<(), String> {
    if bytes.is_empty() {
        return Ok(());
    }
    match writer.write(bytes.as_slices().0) {
        Ok(0) => Err("SSH stream write returned zero".into()),
        Ok(count) => {
            bytes.drain(..count);
            Ok(())
        }
        Err(e)
            if matches!(
                e.kind(),
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
            ) =>
        {
            Ok(())
        }
        Err(e) => Err(e.to_string()),
    }
}

pub(super) fn pump(
    remote: bool,
    lease_ms: u64,
    mut wire_in: impl Read + AsRawFd,
    mut wire_out: impl Write + AsRawFd,
    mut source: impl Read + AsRawFd,
    destination: impl Write + AsRawFd,
    retirement: &mut Option<Instant>,
) -> Result<(), String> {
    for fd in [
        wire_in.as_raw_fd(),
        wire_out.as_raw_fd(),
        source.as_raw_fd(),
        destination.as_raw_fd(),
    ] {
        nonblocking(fd)?;
    }
    let mut destination = Some(destination);
    let lease = Duration::from_millis(lease_ms);
    let heartbeat = lease / 6;
    let mut deadline = Instant::now() + lease;
    let mut last_response = Instant::now();
    let mut next_ping = Instant::now() + heartbeat;
    let mut pending_ping = None;
    let mut encoded = VecDeque::new();
    let mut incoming = Vec::new();
    let mut application = VecDeque::new();
    let mut sent = 0u64;
    let mut acknowledged = 0u64;
    let mut received = 0u64;
    let mut delivered = 0u64;
    let mut advertised = 0u64;
    let mut source_closed = false;
    let mut peer_closed = false;
    let mut end_sent = false;
    let mut ended_sent = false;
    let mut finished = false;
    loop {
        let now = Instant::now();
        if (remote && peer_closed) || (!remote && source_closed) {
            retirement.get_or_insert(now + Duration::from_secs(8));
        }
        if retirement.is_some_and(|deadline| now >= deadline) {
            return Err("SSH retirement deadline exceeded; cleanup is unconfirmed".into());
        }
        if encoded.len() > LIMIT * 2 {
            return Err("SSH control output capacity exhausted".into());
        }
        if now >= deadline {
            return Err("SSH controller lease expired; remote retirement is unconfirmed".into());
        }
        if encoded.is_empty() {
            if remote && pending_ping.is_none() && now >= next_ping {
                let token = challenge()?;
                encoded.extend(frame(PING, &token));
                pending_ping = Some(token);
            } else if advertised != delivered {
                encoded.extend(frame(ACK, &delivered.to_be_bytes()));
                advertised = delivered;
            } else if peer_closed && application.is_empty() && !ended_sent {
                destination.take();
                encoded.extend(frame(ENDED, &[]));
                ended_sent = true;
            } else if source_closed && !end_sent {
                encoded.extend(frame(END, &sent.to_be_bytes()));
                end_sent = true;
            }
        }
        if !remote && ended_sent && encoded.is_empty() {
            return Ok(());
        }
        if remote && finished && encoded.is_empty() {
            return Ok(());
        }
        let wake = if remote && pending_ping.is_none() {
            deadline.min(next_ping)
        } else {
            deadline
        };
        let wake = retirement.map_or(wake, |retirement| wake.min(retirement));
        // A partially buffered frame never postpones the absolute lease deadline.
        let events = match poll(
            &[
                (wire_in.as_raw_fd(), libc::POLLIN),
                (
                    if encoded.is_empty() {
                        -1
                    } else {
                        wire_out.as_raw_fd()
                    },
                    libc::POLLOUT,
                ),
                (
                    if !source_closed
                        && (remote || !peer_closed)
                        && encoded.is_empty()
                        && sent - acknowledged < WINDOW as u64
                    {
                        source.as_raw_fd()
                    } else {
                        -1
                    },
                    libc::POLLIN,
                ),
                (
                    if application.is_empty() {
                        -1
                    } else {
                        destination.as_ref().map_or(-1, AsRawFd::as_raw_fd)
                    },
                    libc::POLLOUT,
                ),
                (
                    if remote || source_closed {
                        -1
                    } else {
                        source.as_raw_fd()
                    },
                    0,
                ),
            ],
            Some(wake),
        ) {
            Ok(events) => events,
            Err(_) if Instant::now() >= wake => continue,
            Err(error) => return Err(error),
        };
        if events[1] != 0 {
            write(&mut wire_out, &mut encoded)?;
        }
        if events[4] != 0 && events[2] == 0 {
            source_closed = true;
        }
        if events[3] != 0 {
            let before = application.len();
            write(
                destination.as_mut().ok_or("SSH application input closed")?,
                &mut application,
            )?;
            delivered += (before - application.len()) as u64;
        }
        if events[2] != 0 {
            let mut bytes = [0; BLOCK];
            let capacity = BLOCK.min(WINDOW - (sent - acknowledged) as usize);
            match source.read(&mut bytes[..capacity]) {
                Ok(0) => source_closed = true,
                Ok(count) => {
                    let mut data = sent.to_be_bytes().to_vec();
                    data.extend(&bytes[..count]);
                    sent += count as u64;
                    encoded.extend(frame(DATA, &data));
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                    ) => {}
                Err(e) => return Err(e.to_string()),
            }
        }
        if events[0] != 0 {
            let mut bytes = [0; LIMIT + 5];
            // Read only the current frame. Control cannot accumulate behind an
            // unbounded queue of data frames or an incomplete peer header.
            let needed = if incoming.len() < 5 {
                5 - incoming.len()
            } else {
                let length =
                    u32::from_be_bytes(incoming[1..5].try_into().expect("length")) as usize;
                if length > LIMIT {
                    return Err("SSH lease frame exceeds 32 KiB".into());
                }
                5 + length - incoming.len()
            };
            if needed > 0 {
                match wire_in.read(&mut bytes[..needed]) {
                    Ok(0) => return Err("SSH connection closed before stream retirement".into()),
                    Ok(count) => incoming.extend(&bytes[..count]),
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::Interrupted
                        ) => {}
                    Err(e) => return Err(e.to_string()),
                }
            }
            if incoming.len() < 5 {
                continue;
            }
            let length = u32::from_be_bytes(incoming[1..5].try_into().expect("length")) as usize;
            if length > LIMIT {
                return Err("SSH lease frame exceeds 32 KiB".into());
            }
            if incoming.len() != length + 5 {
                continue;
            }
            let body = &incoming[5..];
            match incoming[0] {
                DATA if !peer_closed && body.len() > 8 && body.len() <= BLOCK + 8 => {
                    let offset = u64::from_be_bytes(body[..8].try_into().expect("offset"));
                    if offset != received || application.len() + body.len() - 8 > WINDOW {
                        return Err("invalid SSH data sequence or exhausted receive window".into());
                    }
                    application.extend(&body[8..]);
                    received += (body.len() - 8) as u64;
                }
                ACK if body.len() == 8 => {
                    let offset = u64::from_be_bytes(body.try_into().expect("offset"));
                    if offset < acknowledged || offset > sent {
                        return Err("invalid SSH ingestion acknowledgment".into());
                    }
                    acknowledged = offset;
                }
                PING if !remote && body.len() == 32 => {
                    encoded.extend(frame(PONG, body));
                    // This challenge proves receipt of the previous response.
                    // Its local enqueue time precedes the remote renewal; an
                    // arbitrarily delayed challenge cannot move that bound.
                    deadline = last_response + lease;
                    last_response = Instant::now();
                }
                PONG if remote && pending_ping.as_deref() == Some(body) => {
                    pending_ping = None;
                    deadline = Instant::now() + lease;
                    next_ping = Instant::now() + heartbeat;
                }
                END if body.len() == 8 && !peer_closed => {
                    if u64::from_be_bytes(body.try_into().expect("offset")) != received {
                        return Err("invalid SSH end offset".into());
                    }
                    peer_closed = true;
                    if remote {
                        // Controller input closure cancels pending work, even
                        // when the application has stopped reading its pipe.
                        application.clear();
                        destination.take();
                    }
                }
                ENDED if body.is_empty() && end_sent => {
                    if remote {
                        finished = true;
                    }
                }
                _ => return Err("unexpected SSH lease frame".into()),
            }
            incoming.clear();
        }
    }
}
