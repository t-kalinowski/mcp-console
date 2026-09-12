//! Bounded stream retention. IDs and hash-chain anchors cover every byte,
//! including partial inner frames, controls, and terminal preparation results.

use std::collections::VecDeque;
use std::io::{self, Write};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use super::{BLOCK, PACKETS, Secret, WINDOW, invalid};

#[derive(Clone, Copy, Default, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(super) struct Cursor {
    pub id: u64,
    pub hash: Secret,
}

#[derive(Clone)]
struct Packet {
    cursor: Cursor,
    previous: Secret,
    kind: u8,
    bytes: Vec<u8>,
    written: usize,
}

impl Packet {
    fn new(previous: Cursor, kind: u8, bytes: Vec<u8>) -> Self {
        let id = previous.id + 1;
        let hash = Sha256::new()
            .chain_update(previous.hash)
            .chain_update(id.to_be_bytes())
            .chain_update([kind])
            .chain_update(&bytes)
            .finalize()
            .into();
        Self {
            cursor: Cursor { id, hash },
            previous: previous.hash,
            kind,
            bytes,
            written: 0,
        }
    }
    fn encode(&self) -> Vec<u8> {
        let mut body = self.cursor.id.to_be_bytes().to_vec();
        body.extend(self.previous);
        body.push(self.kind);
        body.extend(&self.bytes);
        body
    }
}

#[derive(Default)]
pub(super) struct Flow {
    tx: VecDeque<Packet>,
    rx: VecDeque<Packet>,
    receipts: VecDeque<Cursor>,
    sent: Cursor,
    acknowledged: Cursor,
    accepted: Cursor,
    pub delivered: Cursor,
    next: u64,
    retained: usize,
    pending: usize,
    ended: bool,
}

impl Flow {
    pub fn capacity(&self) -> usize {
        if self.tx.len() >= PACKETS {
            0
        } else {
            BLOCK.min(WINDOW - self.retained)
        }
    }
    pub fn push(&mut self, kind: u8, bytes: Vec<u8>) {
        assert!(!bytes.is_empty() && bytes.len() <= self.capacity());
        self.retained += bytes.len();
        let packet = Packet::new(self.sent, kind, bytes);
        self.sent = packet.cursor;
        self.tx.push_back(packet);
    }
    pub fn send(&mut self) -> Option<Vec<u8>> {
        let next = self.next.max(self.acknowledged.id + 1);
        let packet = self.tx.iter().find(|packet| packet.cursor.id == next)?;
        self.next = next + 1;
        Some(packet.encode())
    }
    pub fn transmitted(&self) -> bool {
        self.next > self.sent.id || self.sent.id == 0
    }
    pub fn end(&self) -> Cursor {
        self.sent
    }
    pub fn accept_end(&mut self, end: Cursor) -> io::Result<()> {
        if end != self.accepted {
            return Err(invalid("invalid SSH terminal stream cursor"));
        }
        self.ended = true;
        Ok(())
    }
    pub fn acknowledge(&mut self, cursor: Cursor) -> io::Result<()> {
        if cursor == self.acknowledged {
            return Ok(());
        }
        if !self.tx.iter().any(|p| p.cursor == cursor) {
            return Err(invalid(
                "invalid SSH ingestion acknowledgment or lost replay history",
            ));
        }
        while self.tx.front().is_some_and(|p| p.cursor.id <= cursor.id) {
            self.retained -= self.tx.pop_front().expect("retained packet").bytes.len();
        }
        self.acknowledged = cursor;
        Ok(())
    }
    pub fn resume(&mut self, cursor: Cursor) -> io::Result<()> {
        self.acknowledge(cursor)?;
        self.next = cursor.id + 1;
        Ok(())
    }
    pub fn receive(&mut self, body: &[u8], remote: bool) -> io::Result<()> {
        if body.len() <= 41
            || body.len() > BLOCK + 41
            || (body[40] != 0 && (remote || body[40] != 1))
        {
            return Err(invalid("invalid SSH stream packet"));
        }
        let id = u64::from_be_bytes(body[..8].try_into().expect("packet id"));
        let previous: Secret = body[8..40].try_into().expect("packet hash");
        // A reconnect can resend accepted bytes still queued for ingestion.
        // Their identity must match; delivery of them is never repeated.
        if id <= self.accepted.id {
            // Delivery can advance after the resume cursor was sent. Retain
            // one bounded sender window of content receipts so that replay
            // crossing that acknowledgment is verified without writing twice.
            if let Some(receipt) = self.receipts.iter().find(|cursor| cursor.id == id) {
                let repeated = Packet::new(
                    Cursor {
                        id: id - 1,
                        hash: previous,
                    },
                    body[40],
                    body[41..].to_vec(),
                );
                if repeated.cursor == *receipt {
                    return Ok(());
                }
            }
            return Err(invalid(
                "stale SSH packet or identity reused with different content",
            ));
        }
        if self.ended {
            return Err(invalid("new SSH data after terminal cursor"));
        }
        if id != self.accepted.id + 1 || previous != self.accepted.hash {
            return Err(invalid("invalid SSH stream sequence or content identity"));
        }
        if self.rx.len() == PACKETS || self.pending + body.len() - 41 > WINDOW {
            return Err(invalid("SSH replay receive capacity exhausted"));
        }
        let packet = Packet::new(self.accepted, body[40], body[41..].to_vec());
        self.accepted = packet.cursor;
        self.receipts.push_back(packet.cursor);
        if self.receipts.len() > PACKETS {
            self.receipts.pop_front();
        }
        self.pending += packet.bytes.len();
        self.rx.push_back(packet);
        Ok(())
    }
    pub fn destination(&self) -> Option<u8> {
        self.rx.front().map(|p| p.kind)
    }
    pub fn deliver(&mut self, writer: &mut impl Write) -> io::Result<()> {
        let Some(packet) = self.rx.front_mut() else {
            return Ok(());
        };
        match writer.write(&packet.bytes[packet.written..]) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => packet.written += count,
            Err(error) if super::stream::would_block(&error) => return Ok(()),
            Err(error) => return Err(error),
        }
        if packet.written == packet.bytes.len() {
            self.delivered = packet.cursor;
            self.pending -= packet.bytes.len();
            self.rx.pop_front();
        }
        Ok(())
    }
    pub fn discard_input(&mut self) {
        self.rx.clear();
        self.pending = 0;
    }
}
