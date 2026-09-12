//! Ownership begins before create; exec exit is never a microVM cleanup receipt.
use super::*;
use crate::target_launch::process::Cancel;
use crate::target_launch::transfer::{Io, duplicate};
use crate::target_launch::{Hello, SandboxIdentity};

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Request {
    pub session: Session,
    pub name: String,
    pub probe: bool,
    pub bootstrap: Bootstrap,
}

pub(super) fn run() -> Result<(), String> {
    let mut input = Io::new(duplicate(0)?, None, Some(Instant::now() + COMMAND_TIMEOUT))?;
    let bytes = target_launch::read_payload(&mut input, target_launch::MAX_BOOTSTRAP, PROTOCOL)
        .map_err(|error| error.to_string())?;
    let request: Request = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid Docker Sandbox owner request: {error}"))?;
    target_launch::owner::run(PROTOCOL, |cancel| {
        let mut vm = Vm {
            request: &request,
            identity: None,
            attempted: false,
            acknowledged: false,
        };
        let result = (|| {
            vm.create(cancel)?;
            cancel.check()?;
            let identity = vm.identity.as_ref().expect("created microVM");
            let mut command = Command::new("sbx");
            command.args([
                "exec",
                "-i",
                "--workdir",
                &request.session.target.workspace,
                &identity.name,
            ]);
            command
                .args(request.session.target.command())
                .arg(if request.probe {
                    "docker-sandbox-probe"
                } else {
                    "docker-sandbox-launch"
                });
            target_launch::owner::attach(
                command,
                &request.bootstrap,
                Hello {
                    version: target_launch::VERSION,
                    build: env!("CARGO_PKG_VERSION").into(),
                    container_id: None,
                    sandbox: Some(identity.clone()),
                },
                PROTOCOL,
                cancel,
            )
        })();
        target_launch::owner::outcome(result, vm.retire())
    })
}

#[derive(Deserialize)]
struct Listing {
    sandboxes: Vec<Sandbox>,
}

#[derive(Deserialize)]
struct Sandbox {
    #[serde(flatten)]
    identity: SandboxIdentity,
    #[serde(default)]
    workspaces: Vec<String>,
}

fn list(cancel: &Cancel) -> Result<Vec<Sandbox>, String> {
    let mut command = Command::new("sbx");
    command.args(["ls", "--json"]);
    let bytes = process::run(
        command,
        cancel,
        Some(Instant::now() + Duration::from_secs(2)),
        process::OutputMode::Data,
        None,
    )?;
    let listing: Listing = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid Docker Sandbox listing: {error}"))?;
    Ok(listing.sandboxes)
}

struct Vm<'a> {
    request: &'a Request,
    identity: Option<SandboxIdentity>,
    attempted: bool,
    acknowledged: bool,
}

impl Vm<'_> {
    fn create(&mut self, cancel: &Cancel) -> Result<(), String> {
        if list(cancel)?
            .iter()
            .any(|vm| vm.identity.name == self.request.name)
        {
            return Err(
                "Docker Sandbox ownership name already exists; refusing to adopt or remove it"
                    .into(),
            );
        }
        let Compute::DockerSandbox(config) = &self.request.session.target.compute else {
            unreachable!()
        };
        let mut command = Command::new("sbx");
        command.args([
            "create",
            "--quiet",
            "--name",
            &self.request.name,
            "--template",
            &config.template,
            "shell",
        ]);
        for mount in &config.mounts {
            let mut argument = mount
                .source
                .to_str()
                .expect("captured UTF-8 source")
                .to_string();
            if matches!(mount.access, Access::ReadOnly) {
                argument.push_str(":ro");
            }
            command.arg(argument);
        }
        // Even a failed or cancelled CLI can leave a create in flight at sandboxd.
        self.attempted = true;
        process::run(command, cancel, Some(Instant::now() + Duration::from_secs(25)), process::OutputMode::Diagnostics, None)
            .map_err(|error| format!("{error}; Docker Sandbox create used shared sources {:?}; shared paths must already exist. Setup requires login, initialized provider policy, an available template, and virtualization; see docs/DOCKER_SANDBOX.md", config.mounts.iter().map(|mount| &mount.source).collect::<Vec<_>>()))?;
        self.acknowledged = true;
        let sandbox = list(cancel)?
            .into_iter()
            .find(|vm| vm.identity.name == self.request.name)
            .ok_or(
                "Docker Sandbox create succeeded but its owned microVM is absent from the listing",
            )?;
        self.identity = Some(sandbox.identity);
        let expected: Vec<_> = config
            .mounts
            .iter()
            .map(|mount| match mount.access {
                Access::ReadOnly => format!("{}:ro", mount.target),
                Access::ReadWrite => mount.target.clone(),
            })
            .collect();
        if sandbox.workspaces != expected {
            return Err(format!(
                "Docker Sandbox resolved shared paths to {:?}; mounts[].target must use the provider's same absolute paths",
                sandbox.workspaces
            ));
        }
        Ok(())
    }

    fn retire(&mut self) -> Result<(), String> {
        if !self.attempted {
            return Ok(());
        }
        let cancel = Cancel::new(PROTOCOL)?;
        let result = (|| {
            let present = list(&cancel)?;
            let selected = present
                .into_iter()
                .find(|vm| vm.identity.name == self.request.name);
            if let Some(sandbox) = selected {
                if self
                    .identity
                    .as_ref()
                    .is_some_and(|identity| identity.id != sandbox.identity.id)
                {
                    return Err(
                        "owned name now identifies a different microVM; refusing removal".into(),
                    );
                }
                // The random name was absent before our one create request. A
                // returned identity can identify partial creation after client loss.
                self.identity = Some(sandbox.identity);
            } else if self.identity.is_some() || self.acknowledged {
                return Ok(());
            } else {
                return Err("creation returned no identity; an empty listing cannot confirm retirement of an unacknowledged creation".into());
            }
            let identity = self.identity.as_ref().expect("owned microVM identity");
            let mut remove = Command::new("sbx");
            remove.args(["rm", "--force", &identity.name]);
            let removed = process::run(
                remove,
                &cancel,
                Some(Instant::now() + Duration::from_secs(10)),
                process::OutputMode::Diagnostics,
                None,
            );
            if list(&cancel)?
                .iter()
                .any(|vm| vm.identity.id == identity.id || vm.identity.name == identity.name)
            {
                return Err(format!(
                    "microVM remains after forced removal: {}",
                    removed.err().unwrap_or_default()
                ));
            }
            if !self.acknowledged {
                return Err("removal was observed, but creation was not acknowledged and may still be in flight".into());
            }
            Ok(())
        })();
        result.map_err(|error: String| {
            format!(
                "Docker Sandbox microVM '{}' ({}) retirement is unconfirmed: {error}",
                self.request.name,
                self.identity
                    .as_ref()
                    .map_or("creation ID unavailable", |identity| identity.id.as_str())
            )
        })
    }
}
