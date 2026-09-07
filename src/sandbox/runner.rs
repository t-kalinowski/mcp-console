//! Bootstrap the private executable, then transfer the original stdin directly.

use super::{installation, platform::TemporaryDirectory};
use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::fs::File;
use std::io::{self, Read as _, Write as _};
use std::os::fd::{AsRawFd as _, BorrowedFd, FromRawFd as _, OwnedFd, RawFd};
use std::os::unix::net::UnixStream;
use std::process::{Command, Stdio};

const RELEASE: u8 = 1;
const POLICY_EXTENSION: &str = include_str!("policy_extensions.sbpl");

pub(super) struct StartupGate {
    owner: UnixStream,
    input: Option<OwnedFd>,
    bootstrap: Vec<u8>,
}

impl StartupGate {
    pub(super) fn new(
        command: &mut Command,
        temporary: &TemporaryDirectory,
        program: &OsStr,
        arguments: &[OsString],
    ) -> Result<Self, String> {
        // Rust startup reopens missing standard descriptors with /dev/null
        // before main, including when the caller starts with stdin closed.
        let input = unsafe { BorrowedFd::borrow_raw(libc::STDIN_FILENO) }
            .try_clone_to_owned()
            .map_err(|error| format!("failed to retain target standard input: {error}"))?;
        let (target, owner) = UnixStream::pair()
            .map_err(|error| format!("failed to create the sandbox startup gate: {error}"))?;
        command.stdin(Stdio::from(OwnedFd::from(target)));
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox target gate: {error}"))?;
        let mut target = vec![
            utf8(executable.as_os_str())?,
            "sandbox-target".to_string(),
            "--gate-fd".to_string(),
            "0".to_string(),
            "--".to_string(),
            utf8(program)?,
        ];
        target.extend(
            arguments
                .iter()
                .map(|argument| utf8(argument))
                .collect::<Result<Vec<_>, _>>()?,
        );
        let mut environment = std::env::vars_os()
            .map(|(name, value)| Ok((utf8(&name)?, utf8(&value)?)))
            .collect::<Result<BTreeMap<_, _>, String>>()?;
        for (name, value) in command.get_envs() {
            let name = utf8(name)?;
            if let Some(value) = value {
                environment.insert(name, utf8(value)?);
            } else {
                environment.remove(&name);
            }
        }
        environment.insert("TMPDIR".to_string(), utf8(temporary.path().as_os_str())?);
        // This directory is disposable data, not a native writable workspace
        // anchor: its metadata directories and the root itself may be replaced.
        let temporary_literal = utf8(temporary.path().as_os_str())?
            .replace('\\', "\\\\")
            .replace('"', "\\\"");
        let extension =
            format!("{POLICY_EXTENSION}\n(allow file-write* (subpath \"{temporary_literal}\"))\n");
        let payload = serde_json::to_vec(&serde_json::json!({
            "version": installation::PROTOCOL_VERSION,
            "command": target,
            "cwd": std::env::current_dir().map_err(|error| format!("failed to read sandbox working directory: {error}"))?,
            "environment": environment,
            "filesystem": {"kind": "restricted", "entries": [
                {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"}
            ]},
            "network": "restricted",
            "proxy": null,
            "macos_seatbelt_profile_extension": extension,
        })).map_err(|error| format!("failed to encode sandbox startup: {error}"))?;
        if payload.len() > 1_048_576 {
            return Err(
                "sandbox startup exceeds the private executable's message limit".to_string(),
            );
        }
        let mut bootstrap = (payload.len() as u32).to_be_bytes().to_vec();
        bootstrap.extend(payload);
        Ok(Self {
            owner,
            input: Some(input),
            bootstrap,
        })
    }

    pub(super) fn input_descriptor(&self) -> RawFd {
        self.input
            .as_ref()
            .expect("unreleased target input")
            .as_raw_fd()
    }

    pub(super) fn release(&mut self, original_mask: libc::sigset_t) -> io::Result<()> {
        // Withhold the entire bootstrap until the host manager owns this lifetime.
        self.owner.write_all(&self.bootstrap)?;
        let mut payload = vec![RELEASE];
        payload.extend(original_mask.to_ne_bytes());
        let mut vector = libc::iovec {
            iov_base: payload.as_mut_ptr().cast(),
            iov_len: payload.len(),
        };
        let mut control = [0usize; 2];
        let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
        message.msg_iov = &mut vector;
        message.msg_iovlen = 1;
        message.msg_control = control.as_mut_ptr().cast();
        message.msg_controllen = unsafe { libc::CMSG_SPACE(size_of::<RawFd>() as _) };
        unsafe {
            let header = libc::CMSG_FIRSTHDR(&message);
            (*header).cmsg_level = libc::SOL_SOCKET;
            (*header).cmsg_type = libc::SCM_RIGHTS;
            (*header).cmsg_len = libc::CMSG_LEN(size_of::<RawFd>() as _);
            libc::CMSG_DATA(header)
                .cast::<RawFd>()
                .write_unaligned(self.input_descriptor());
        }
        let count = loop {
            let count = unsafe { libc::sendmsg(self.owner.as_raw_fd(), &message, 0) };
            if count >= 0 {
                break count as usize;
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        };
        if count == 0 {
            return Err(io::ErrorKind::WriteZero.into());
        }
        self.owner.write_all(&payload[count..])?;
        drop(self.input.take());
        Ok(())
    }
}

fn utf8(value: &OsStr) -> Result<String, String> {
    value.to_str().map(str::to_owned).ok_or_else(|| {
        "sandbox startup requires UTF-8 arguments, paths, and environment values".to_string()
    })
}

pub(super) fn restore_target_input() -> Result<bool, String> {
    restore_input().map_err(|error| format!("failed to receive sandbox target startup: {error}"))
}

fn restore_input() -> io::Result<bool> {
    // The native executable has consumed its frame and transferred this socket
    // as fd 0. It carries only the caller's descriptor handoff, never user input.
    let mut gate = unsafe { File::from_raw_fd(libc::STDIN_FILENO) };
    let mut payload = [0u8; 1 + size_of::<libc::sigset_t>()];
    let mut vector = libc::iovec {
        iov_base: payload.as_mut_ptr().cast(),
        iov_len: payload.len(),
    };
    let mut control = [0usize; 2];
    let mut message: libc::msghdr = unsafe { std::mem::zeroed() };
    message.msg_iov = &mut vector;
    message.msg_iovlen = 1;
    message.msg_control = control.as_mut_ptr().cast();
    message.msg_controllen = unsafe { libc::CMSG_SPACE(size_of::<RawFd>() as _) };
    let count = loop {
        let count = unsafe { libc::recvmsg(gate.as_raw_fd(), &mut message, 0) };
        if count >= 0 {
            break count as usize;
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    };
    if count == 0 {
        return Ok(false);
    }
    let header = unsafe { libc::CMSG_FIRSTHDR(&message) };
    if header.is_null()
        || unsafe {
            (*header).cmsg_level != libc::SOL_SOCKET
                || (*header).cmsg_type != libc::SCM_RIGHTS
                || (*header).cmsg_len != libc::CMSG_LEN(size_of::<RawFd>() as _)
        }
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "missing target input descriptor",
        ));
    }
    let input =
        unsafe { OwnedFd::from_raw_fd(libc::CMSG_DATA(header).cast::<RawFd>().read_unaligned()) };
    if message.msg_flags & libc::MSG_CTRUNC != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "truncated target input descriptor",
        ));
    }
    gate.read_exact(&mut payload[count..])?;
    if payload[0] != RELEASE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid startup release",
        ));
    }
    let original_mask =
        libc::sigset_t::from_ne_bytes(payload[1..].try_into().expect("signal mask bytes"));
    drop(gate);
    loop {
        if unsafe { libc::dup2(input.as_raw_fd(), libc::STDIN_FILENO) } >= 0 {
            break;
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
    drop(input);
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &original_mask, std::ptr::null_mut()) };
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result));
    }
    Ok(true)
}
