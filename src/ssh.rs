//! Authenticated launch framing around the unchanged relay JSONL stream.

use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub(crate) use crate::target_launch::Retirement;
#[cfg(unix)]
use crate::target_launch::transfer as launch_io;
use crate::target_launch::{Bootstrap, MAX_BOOTSTRAP, VERSION};
pub(crate) mod preparation;

pub(crate) const SETUP_TIMEOUT: Duration = Duration::from_secs(30);
#[derive(Clone)]
pub(crate) struct Session {
    pub target: crate::settings::Target,
    roots: Vec<PathBuf>,
    blocked: Arc<Mutex<Option<String>>>,
    pub preparation: Option<preparation::Preparation>,
    discovery: Option<preparation::Discovery>,
}

impl Session {
    pub fn new(target: crate::settings::Target, roots: Vec<PathBuf>) -> Self {
        Self {
            target,
            roots,
            blocked: Arc::default(),
            preparation: None,
            discovery: None,
        }
    }

    pub fn metadata(&self) -> serde_json::Value {
        serde_json::json!({"transport": self.target.transport, "workspace": self.target.workspace})
    }

    pub fn command(&self) -> Result<Command, String> {
        self.command_for("ssh-launch")
    }

    fn command_for(&self, operation: &str) -> Result<Command, String> {
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| "SSH session lock poisoned")?
        {
            return Err(error.clone());
        }
        // OpenSSH joins remote argv with spaces and passes it through a shell.
        // Only the trusted executable prefix goes there; all launch data uses stdin.
        let remote = self
            .target
            .command()
            .iter()
            .map(|argument| format!("'{}'", argument.replace('\'', "'\\''")))
            .chain(std::iter::once(format!("'{operation}'")))
            .collect::<Vec<_>>()
            .join(" ");
        let mut command = Command::new("ssh");
        command.args([
            "-T",
            "-a",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "--",
            self.target.host(),
            &remote,
        ]);
        Ok(command)
    }

    pub fn bootstrap(
        &self,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        managed_r: Option<&crate::resolver::ManagedR>,
        python: Option<&crate::resolver::ManagedPython>,
    ) -> Result<Vec<u8>, String> {
        let value = serde_json::to_vec(&Bootstrap {
            version: VERSION,
            build: env!("CARGO_PKG_VERSION").into(),
            workspace: self.target.workspace.clone(),
            policy: policy.clone(),
            writable_roots: self.roots.clone(),
            no_sandbox,
            environment: self
                .discovery
                .clone()
                .map(|discovery| preparation::WorkerEnvironment {
                    discovery,
                    r: managed_r.cloned(),
                    python: python.cloned(),
                }),
        })
        .map_err(|error| format!("cannot encode SSH bootstrap: {error}"))?;
        if value.len() > MAX_BOOTSTRAP {
            return Err("SSH bootstrap exceeds 1 MiB".into());
        }
        let mut bytes = (value.len() as u32).to_be_bytes().to_vec();
        bytes.extend(value);
        Ok(bytes)
    }

    pub fn discover(
        &mut self,
        policy: &crate::settings::SandboxSettings,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<preparation::Discovery, String> {
        let selections = preparation::Selections::from_policy(policy);
        let (preparation, discovery) =
            preparation::Preparation::open(self, selections, on_started)?;
        self.preparation = Some(preparation);
        self.discovery = Some(discovery.clone());
        Ok(discovery)
    }

    pub fn check_retirement(&self, retirement: &Retirement) -> Result<(), String> {
        retirement.check().map_err(|_| {
            self.block();
            "remote retirement is unconfirmed (SSH exit is not a cleanup barrier)".into()
        })
    }

    fn block(&self) {
        let mut blocked = self
            .blocked
            .lock()
            .expect("SSH session lock is not poisoned");
        blocked.get_or_insert_with(|| format!(
            "SSH target '{}' retirement is unconfirmed; this session cannot start a replacement",
            self.target.host(),
        ));
    }
}

pub(crate) fn run() -> Result<(), String> {
    crate::target_launch::run(crate::target_launch::Protocol("SSH"), false, false)
}

fn read_payload(reader: &mut impl std::io::Read, maximum: usize) -> std::io::Result<Vec<u8>> {
    crate::target_launch::read_payload(reader, maximum, crate::target_launch::Protocol("SSH"))
}
