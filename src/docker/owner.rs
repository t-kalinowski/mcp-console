//! One ordinary Docker container, owned independently of its attachment.
use crate::target_launch::process::{self, Cancel};
use crate::target_launch::transfer::{Io, duplicate};
use crate::target_launch::{self, Hello};
use std::time::{Duration, Instant};

type Request = target_launch::owner::Request<super::Captured>;

pub(super) fn run() -> Result<(), String> {
    let mut input = Io::new(
        duplicate(0)?,
        None,
        Some(Instant::now() + super::COMMAND_TIMEOUT),
    )?;
    let bytes =
        target_launch::read_payload(&mut input, target_launch::MAX_BOOTSTRAP, super::PROTOCOL)
            .map_err(|e| e.to_string())?;
    let request: Request =
        serde_json::from_slice(&bytes).map_err(|e| format!("invalid Docker owner request: {e}"))?;
    target_launch::owner::run(super::PROTOCOL, |cancel| {
        let mut container = Container {
            session: &request.session,
            name: &request.name,
            id: None,
            retired: false,
        };
        let result = (|| {
            container.create(cancel, request.probe)?;
            cancel.check()?;
            let mut command = container.session.endpoint.command();
            command.args([
                "container",
                "start",
                "--attach",
                "--interactive",
                "--",
                container.id.as_ref().expect("created ID"),
            ]);
            target_launch::owner::attach(
                command,
                &request.bootstrap,
                Hello {
                    container_id: container.id.clone(),
                    sandbox: None,
                    version: target_launch::VERSION,
                    build: env!("CARGO_PKG_VERSION").into(),
                },
                super::PROTOCOL,
                cancel,
            )
        })();
        target_launch::owner::outcome(result, container.retire())
    })
}

struct Container<'a> {
    session: &'a super::Captured,
    name: &'a str,
    id: Option<String>,
    retired: bool,
}

impl Container<'_> {
    fn create(&mut self, cancel: &Cancel, probe: bool) -> Result<(), String> {
        let crate::settings::Compute::Docker(docker) = &self.session.target.compute else {
            unreachable!()
        };
        let prefix = self.session.target.command();
        let mut command = self.session.endpoint.command();
        command.args([
            "container",
            "create",
            "--name",
            self.name,
            "--label",
            &format!("{}={}", super::LABEL, self.name),
            "--interactive",
            "--init",
            "--no-healthcheck",
            "--restart=no",
            "--network=bridge",
            "--stop-signal=SIGTERM",
            "--stop-timeout=1",
            "--workdir=/",
            "--entrypoint",
            &prefix[0],
        ]);
        for mount in &docker.mounts {
            command.arg("--mount").arg(mount.argument());
        }
        if let Some(user) = &docker.user {
            command.arg("--user").arg(user);
        }
        command
            .arg("--")
            .arg(&self.session.image)
            .args(&prefix[1..])
            .arg(if probe {
                "docker-probe"
            } else {
                "docker-launch"
            });
        let bytes = process::run(
            command,
            cancel,
            Some(Instant::now() + super::COMMAND_TIMEOUT),
            process::OutputMode::Capture,
            None,
        )?;
        let id = String::from_utf8(bytes)
            .map_err(|e| e.to_string())?
            .trim()
            .to_string();
        if id.len() != 64 || !id.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("Docker create did not return a full container ID".into());
        }
        self.id = Some(id);
        Ok(())
    }

    fn list(&self, cancel: &Cancel) -> Result<Vec<String>, String> {
        let filter = match &self.id {
            Some(id) => format!("id={id}"),
            None => format!("label={}={}", super::LABEL, self.name),
        };
        let mut command = self.session.endpoint.command();
        command.args([
            "container",
            "ls",
            "--all",
            "--quiet",
            "--no-trunc",
            "--filter",
            &filter,
        ]);
        let bytes = process::run(
            command,
            cancel,
            Some(Instant::now() + Duration::from_secs(2)),
            process::OutputMode::Capture,
            None,
        )?;
        let text = String::from_utf8(bytes).map_err(|e| e.to_string())?;
        Ok(text.lines().map(str::to_string).collect())
    }

    fn retire(&mut self) -> Result<(), String> {
        if self.retired {
            return Ok(());
        }
        let cancel = Cancel::new(super::PROTOCOL)?;
        let result = (|| {
            if self.id.is_none() {
                let ids = self.list(&cancel)?;
                if ids.len() > 1 {
                    return Err("multiple containers have the ownership token".into());
                }
                self.id = ids.into_iter().next();
                if self.id.is_none() {
                    // A cancelled create request may still be in flight at the
                    // daemon. An empty listing cannot acknowledge that request.
                    return Err("container creation returned no identity; an empty listing cannot confirm retirement of an unacknowledged creation".into());
                }
            }
            if let Some(id) = &self.id {
                let mut stop = self.session.endpoint.command();
                stop.args([
                    "container",
                    "stop",
                    "--signal=SIGTERM",
                    "--timeout=1",
                    "--",
                    id,
                ]);
                let _ = process::run(
                    stop,
                    &cancel,
                    Some(Instant::now() + Duration::from_secs(2)),
                    process::OutputMode::Capture,
                    None,
                );
                let mut remove = self.session.endpoint.command();
                remove.args(["container", "rm", "--force", "--volumes", "--", id]);
                // Only a successful daemon query proving absence is the receipt.
                let removed = process::run(
                    remove,
                    &cancel,
                    Some(Instant::now() + Duration::from_secs(2)),
                    process::OutputMode::Capture,
                    None,
                );
                if !self.list(&cancel)?.is_empty() {
                    return Err(format!(
                        "container remains after removal: {}",
                        removed.err().unwrap_or_default()
                    ));
                }
            }
            Ok(())
        })();
        self.retired = true;
        result.map_err(|error: String| {
            format!(
                "Docker container '{}' ({}) retirement is unconfirmed: {error}",
                self.name,
                self.id.as_deref().unwrap_or("creation ID unavailable")
            )
        })
    }
}

impl Drop for Container<'_> {
    fn drop(&mut self) {
        let _ = self.retire();
    }
}
