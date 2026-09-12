//! Controller sessions and generation receipts for the three selected targets.
use crate::settings::{Compute, Provider, SandboxSettings, Target};
use crate::target_launch::{self, Bootstrap, Protocol, Retirement, process};
use std::io::Read;
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const PROBE_TIMEOUT: Duration = Duration::from_secs(40);

/// Provider-owned command labels, diagnostic routing, and outer-owner allowances.
pub(crate) struct ComputeProfile {
    pub protocol: Protocol,
    pub resource: &'static str,
    pub owner_command: &'static str,
    pub probe_output: process::OutputMode,
    pub probe_retirement_grace: Duration,
    pub retirement_grace: Duration,
}

/// Controller-only state; never serialized into the provider owner's request.
#[derive(Clone)]
pub(crate) struct ComputeState {
    profile: &'static ComputeProfile,
    roots: Vec<PathBuf>,
    blocked: Arc<Mutex<Option<String>>>,
}

#[derive(Clone)]
pub(crate) enum Session {
    Ssh(crate::ssh::Session),
    Docker(crate::docker::Captured, ComputeState),
    DockerSandbox(crate::docker_sandbox::Captured, ComputeState),
}

impl Session {
    pub fn setup_compute(
        target: Target,
        roots: Vec<PathBuf>,
        policy: &SandboxSettings,
        no_sandbox: bool,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let profile = match target.compute {
            Compute::Docker(_) => &crate::docker::PROFILE,
            Compute::DockerSandbox(_) => &crate::docker_sandbox::PROFILE,
            Compute::Host {} => unreachable!("host compute has its own runtime discovery"),
        };
        let cancel = process::Cancel::new(profile.protocol)?;
        started(crate::resolver::ResolverStopHandle::new(cancel.clone()))?;
        let state = ComputeState {
            profile,
            roots,
            blocked: Arc::default(),
        };
        let session = match target.compute {
            Compute::Docker(_) => {
                Self::Docker(crate::docker::Captured::capture(target, &cancel)?, state)
            }
            Compute::DockerSandbox(_) => Self::DockerSandbox(
                crate::docker_sandbox::Captured::capture(target, &cancel)?,
                state,
            ),
            Compute::Host {} => unreachable!(),
        };
        let (command, bytes, generation) = session.launch(policy, no_sandbox, None, None, true)?;
        let bytes = process::run(
            command,
            &cancel,
            Some(Instant::now() + PROBE_TIMEOUT),
            profile.probe_output,
            Some(process::OwnerInput {
                bytes,
                retirement_grace: profile.probe_retirement_grace,
            }),
        )?;
        let mut output = generation.output(std::io::Cursor::new(bytes), None);
        let mut unexpected = Vec::new();
        output
            .read_to_end(&mut unexpected)
            .map_err(|error| error.to_string())?;
        generation.retirement.check()?;
        if !unexpected.is_empty() {
            return Err(format!(
                "unexpected {} runtime probe output",
                profile.protocol.0
            ));
        }
        Ok(session)
    }

    fn compute(&self) -> Option<&ComputeState> {
        match self {
            Self::Ssh(_) => None,
            Self::Docker(_, state) | Self::DockerSandbox(_, state) => Some(state),
        }
    }

    pub fn is_ssh(&self) -> bool {
        matches!(self, Self::Ssh(_))
    }

    pub fn ssh_preparation(&self) -> Option<&crate::ssh::preparation::Preparation> {
        match self {
            Self::Ssh(session) => session.preparation.as_ref(),
            _ => None,
        }
    }

    pub fn provider(&self) -> Provider {
        match self {
            Self::DockerSandbox(..) => Provider::Compute,
            _ => Provider::Native,
        }
    }

    pub fn retirement_grace(&self) -> Duration {
        self.compute()
            .map_or(crate::ssh::RETIREMENT_GRACE, |state| {
                state.profile.retirement_grace
            })
    }

    pub fn protocol(&self) -> Protocol {
        self.compute()
            .map_or(crate::ssh::PROTOCOL, |state| state.profile.protocol)
    }

    pub fn metadata(&self) -> serde_json::Value {
        match self {
            Self::Ssh(session) => session.metadata(),
            Self::Docker(captured, _) => captured.metadata(),
            Self::DockerSandbox(captured, _) => captured.metadata(),
        }
    }

    pub fn launch(
        &self,
        policy: &SandboxSettings,
        no_sandbox: bool,
        managed_r: Option<&crate::resolver::ManagedR>,
        python: Option<&crate::resolver::ManagedPython>,
        probe: bool,
    ) -> Result<(Command, Vec<u8>, Generation), String> {
        let (command, bytes, owner) = match self {
            Self::Ssh(session) => (
                session.command()?,
                session.bootstrap(policy, no_sandbox, managed_r, python)?,
                GenerationOwner::Ssh(Box::new(session.clone())),
            ),
            Self::Docker(captured, state) => state.launch(
                captured,
                &captured.target,
                policy,
                no_sandbox,
                self.provider(),
                probe,
            )?,
            Self::DockerSandbox(captured, state) => state.launch(
                captured,
                &captured.target,
                policy,
                no_sandbox,
                self.provider(),
                probe,
            )?,
        };
        Ok((
            command,
            bytes,
            Generation {
                retirement: Retirement::default(),
                owner,
            },
        ))
    }
}

impl ComputeState {
    fn launch(
        &self,
        captured: &impl serde::Serialize,
        target: &Target,
        policy: &SandboxSettings,
        no_sandbox: bool,
        provider: Provider,
        probe: bool,
    ) -> Result<(Command, Vec<u8>, GenerationOwner), String> {
        let label = self.profile.protocol.0;
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| format!("{label} session lock poisoned"))?
        {
            return Err(error.clone());
        }
        let name = format!("mcp-console-{}", target_launch::owner::token()?);
        let request = target_launch::owner::Request {
            session: captured,
            name: name.clone(),
            probe,
            bootstrap: Bootstrap {
                version: target_launch::VERSION,
                build: env!("CARGO_PKG_VERSION").into(),
                workspace: target.workspace.clone(),
                policy: policy.clone(),
                writable_roots: self.roots.clone(),
                no_sandbox,
                provider,
                environment: None,
            },
        };
        let mut command = Command::new(std::env::current_exe().map_err(|error| error.to_string())?);
        command.arg(self.profile.owner_command);
        Ok((
            command,
            target_launch::encode(&request)?,
            GenerationOwner::Compute(self.clone(), name),
        ))
    }
}

/// Each launched generation retains its receipt and exact ownership name.
#[derive(Clone)]
pub(crate) struct Generation {
    retirement: Retirement,
    owner: GenerationOwner,
}

#[derive(Clone)]
enum GenerationOwner {
    Ssh(Box<crate::ssh::Session>),
    Compute(ComputeState, String),
}

impl Generation {
    pub fn output<R: Read>(
        &self,
        reader: R,
        recording: Option<crate::transcript::Transcript>,
    ) -> target_launch::Output<R> {
        let (protocol, recording) = match &self.owner {
            GenerationOwner::Ssh(_) => (crate::ssh::PROTOCOL, None),
            GenerationOwner::Compute(state, _) => (state.profile.protocol, recording),
        };
        target_launch::Output::new(reader, protocol, self.retirement.clone())
            .with_recording(recording)
    }

    pub fn check_retirement(&self) -> Result<(), String> {
        match &self.owner {
            GenerationOwner::Ssh(session) => session.check_retirement(&self.retirement),
            GenerationOwner::Compute(state, name) => self.retirement.check().map_err(|error| {
                let error = format!(
                    "{} {} '{name}': {error}; this session cannot start a replacement",
                    state.profile.protocol.0, state.profile.resource
                );
                state
                    .blocked
                    .lock()
                    .expect("compute session lock")
                    .get_or_insert(error.clone());
                error
            }),
        }
    }
}
