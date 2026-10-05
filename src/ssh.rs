//! Authenticated launch framing around the unchanged relay JSONL stream.

use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub(crate) use crate::resolver::preparation;
use crate::target_launch::{self, Bootstrap, Protocol, Retirement};

pub(crate) const PROTOCOL: Protocol = Protocol("SSH");
pub(crate) const RETIREMENT_GRACE: Duration = Duration::from_secs(6);
#[derive(Clone)]
pub(crate) struct Session {
    pub target: crate::settings::Target,
    roots: Vec<PathBuf>,
    languages: crate::cell::Languages,
    pub(crate) blocked: Arc<Mutex<Option<String>>>,
    pub preparation: Option<preparation::Preparation>,
    discovery: Option<preparation::Discovery>,
}

impl Session {
    pub fn new(
        target: crate::settings::Target,
        roots: Vec<PathBuf>,
        languages: crate::cell::Languages,
    ) -> Self {
        Self {
            target,
            roots,
            languages,
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

    pub(crate) fn command_for(&self, operation: &str) -> Result<Command, String> {
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
        command
            .args(["-T", "-a"])
            .args(["-o", "BatchMode=yes"])
            .args(["-o", "ConnectTimeout=10"])
            .args(["-o", "ControlMaster=no"])
            .args(["-o", "ControlPersist=no"])
            .args(["-o", "ClearAllForwardings=yes"])
            .args(["-o", "PermitLocalCommand=no"])
            .args(["--", self.target.host(), &remote]);
        Ok(command)
    }

    pub fn bootstrap(
        &self,
        policy: &crate::settings::SandboxSettings,
        no_sandbox: bool,
        managed_r: Option<&crate::resolver::ManagedR>,
        python: Option<&crate::resolver::ManagedPython>,
        native: Option<&crate::local_runtime::Selection>,
    ) -> Result<Vec<u8>, String> {
        target_launch::encode(&Bootstrap {
            python: None,
            languages: self.languages,
            build: env!("CARGO_PKG_VERSION").into(),
            workspace: self.target.workspace.clone(),
            policy: policy.clone(),
            writable_roots: self.roots.clone(),
            no_sandbox,
            provider: crate::settings::Provider::Native,
            environment: self
                .discovery
                .clone()
                .map(|discovery| preparation::WorkerEnvironment {
                    discovery,
                    r: managed_r.cloned(),
                    python: python.cloned(),
                    native: native.cloned(),
                }),
        })
        .map_err(|error| format!("cannot encode SSH bootstrap: {error}"))
    }

    pub fn discover(
        &mut self,
        policy: &crate::settings::SandboxSettings,
        python: Option<&std::path::Path>,
        diagnostics: crate::process_output::Diagnostics,
        on_started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<preparation::Discovery, String> {
        let selections = preparation::Selections::from_policy(policy, python)?;
        let (preparation, discovery) =
            preparation::Preparation::open(self, selections, diagnostics, on_started)?;
        self.preparation = Some(preparation);
        // The session environment owns the current native selection after discovery.
        self.discovery = Some(preparation::Discovery {
            native: None,
            ..discovery.clone()
        });
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
    target_launch::run(PROTOCOL, false, None)
}
