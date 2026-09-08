//! Send one initial configuration frame over the private runner setup pipe.

use super::{installation, platform::TemporaryDirectory};
use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io::{self, PipeWriter, Write as _};
use std::os::fd::{AsRawFd as _, RawFd};
use std::process::Command;

const POLICY_EXTENSION: &str = include_str!("policy_extensions.sbpl");

pub(super) struct Setup {
    writer: Option<PipeWriter>,
    frame: Vec<u8>,
    written: usize,
}

impl Setup {
    pub(super) fn new(
        command: &mut Command,
        temporary: &TemporaryDirectory,
        program: &OsStr,
        arguments: &[OsString],
        original_mask: libc::sigset_t,
    ) -> Result<Self, String> {
        let (reader, writer) =
            io::pipe().map_err(|error| format!("failed to create sandbox setup pipe: {error}"))?;
        let flags = unsafe { libc::fcntl(writer.as_raw_fd(), libc::F_GETFL) };
        if flags < 0
            || unsafe { libc::fcntl(writer.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) }
                < 0
        {
            return Err(format!(
                "failed to configure sandbox setup writes: {}",
                io::Error::last_os_error()
            ));
        }
        command
            .arg("--bootstrap-fd")
            .arg(reader.as_raw_fd().to_string());
        let executable = std::env::current_exe()
            .map_err(|error| format!("failed to locate the sandbox target wrapper: {error}"))?;
        let mut target = vec![
            utf8(executable.as_os_str())?,
            "sandbox-target".to_string(),
            "--signal-mask".to_string(),
            original_mask.to_string(),
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
        let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
        frame.extend(payload);
        // Command owns the read end through spawn. Dropping Command closes
        // the launcher's copy before manager startup; the parent stays CLOEXEC.
        crate::process_descriptors::close_unlisted(command, reader)?;
        Ok(Self {
            writer: Some(writer),
            frame,
            written: 0,
        })
    }

    pub(super) fn descriptor(&self) -> RawFd {
        self.writer
            .as_ref()
            .expect("setup has not started")
            .as_raw_fd()
    }

    pub(super) fn write_once(&mut self) -> io::Result<()> {
        let Some(writer) = &mut self.writer else {
            return Ok(());
        };
        // Return to lifetime events after every write, even when the reader
        // drains fast enough that successive writes would all succeed.
        match writer.write(&self.frame[self.written..]) {
            Ok(0) => return Err(io::ErrorKind::WriteZero.into()),
            Ok(count) => {
                self.written += count;
                if self.written < self.frame.len() {
                    return Ok(());
                }
            }
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::Interrupted | io::ErrorKind::WouldBlock
                ) =>
            {
                return Ok(());
            }
            Err(error) if error.kind() == io::ErrorKind::BrokenPipe => {}
            Err(error) => return Err(error),
        }
        // Closing the completed channel also removes its kqueue write watch.
        // Native setup starts at the complete frame, without requiring EOF.
        self.writer = None;
        Ok(())
    }
}

fn utf8(value: &OsStr) -> Result<String, String> {
    value.to_str().map(str::to_owned).ok_or_else(|| {
        "sandbox startup requires UTF-8 arguments, paths, and environment values".to_string()
    })
}
