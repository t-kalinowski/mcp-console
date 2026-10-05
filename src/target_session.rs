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
    languages: crate::cell::Languages,
    blocked: Arc<Mutex<Option<String>>>,
    // One immutable handoff retained beside the captured image/template.
    runtime: Option<Arc<crate::resolver::preparation::WorkerEnvironment>>,
}

enum ComputeLaunch {
    Probe(Option<String>),
    Worker,
}

#[derive(Clone)]
pub(crate) enum Session {
    Ssh(crate::ssh::Session),
    Docker(crate::docker::Captured, ComputeState),
    DockerSandbox(crate::docker_sandbox::Captured, ComputeState),
}

impl Session {
    #[allow(clippy::too_many_arguments)]
    pub fn setup_compute(
        target: Target,
        roots: Vec<PathBuf>,
        languages: crate::cell::Languages,
        policy: &SandboxSettings,
        no_sandbox: bool,
        python: Option<&std::path::Path>,
        diagnostics: crate::process_output::Diagnostics,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        let profile = match target.compute {
            Compute::Docker(_) => &crate::docker::PROFILE,
            Compute::DockerSandbox(_) => &crate::docker_sandbox::PROFILE,
            Compute::Host {} => unreachable!("host compute has its own runtime discovery"),
        };
        let cancel = process::Cancel::new(profile.protocol)?.with_diagnostics(diagnostics);
        let mut provider_retirement: Option<Retirement> = None;
        let result: Result<Self, target_launch::SetupFailure> = (|| {
            started(crate::resolver::ResolverStopHandle::new(cancel.clone()))?;
            let state = ComputeState {
                profile,
                roots,
                languages,
                blocked: Arc::default(),
                runtime: None,
            };
            let mut session = match target.compute {
                Compute::Docker(_) => {
                    Self::Docker(crate::docker::Captured::capture(target, &cancel)?, state)
                }
                Compute::DockerSandbox(_) => Self::DockerSandbox(
                    crate::docker_sandbox::Captured::capture(target, &cancel)?,
                    state,
                ),
                Compute::Host {} => unreachable!(),
            };
            let configured = python
                .map(|path| {
                    path.to_str()
                        .map(str::to_owned)
                        .ok_or("target python selection is not UTF-8")
                })
                .transpose()?;
            let (command, bytes, generation) = match &session {
                Self::Docker(captured, state) => state.launch(
                    captured,
                    &captured.target,
                    policy,
                    no_sandbox,
                    session.provider(),
                    ComputeLaunch::Probe(configured),
                )?,
                Self::DockerSandbox(captured, state) => state.launch(
                    captured,
                    &captured.target,
                    policy,
                    no_sandbox,
                    session.provider(),
                    ComputeLaunch::Probe(configured),
                )?,
                Self::Ssh(_) => unreachable!(),
            };
            let generation = Generation {
                retirement: Retirement::default(),
                owner: generation,
            };
            let report = process::run_report(
                command,
                &cancel,
                Some(Instant::now() + PROBE_TIMEOUT),
                profile.probe_output,
                Some(process::OwnerInput {
                    bytes,
                    retirement_grace: profile.probe_retirement_grace,
                }),
            )?;
            provider_retirement = Some(generation.retirement.clone());
            let mut output = generation
                .output(std::io::Cursor::new(report.output), None)
                .for_probe();
            let mut unexpected = Vec::new();
            let parsed = output
                .read_to_end(&mut unexpected)
                .map(|_| ())
                .map_err(|error| error.to_string());
            let parsed = if !output.retirement_received()
                && let Some(status) = report.status.filter(|status| !status.success())
            {
                Err(format!(
                    "{} command failed with {status}; {}",
                    profile.protocol.0,
                    parsed.err().unwrap_or_default()
                ))
            } else {
                parsed
            };
            let parsed = if !output.retirement_received() {
                parsed.map_err(|error| {
                    format!(
                        "{error}; {}",
                        generation
                            .retirement
                            .check()
                            .expect_err("missing provider receipt")
                    )
                })
            } else {
                parsed
            };
            // A command failure cannot discard a valid receipt or protocol bytes.
            // Conversely, a receipt confirms only provider retirement, not work.
            match (report.result, parsed) {
                (Err(mut failure), Err(error)) => {
                    failure.error = Some(match failure.error {
                        Some(primary) => format!("{primary}; {error}"),
                        None => error,
                    });
                    return Err(failure);
                }
                (Err(failure), Ok(())) => return Err(failure),
                (Ok(()), Err(error)) => return Err(error.into()),
                (Ok(()), Ok(())) => {}
            }
            generation.retirement.check()?;
            if !unexpected.is_empty() {
                return Err(
                    format!("unexpected {} runtime probe output", profile.protocol.0).into(),
                );
            }
            let runtime = output.take_runtime()?;
            match &mut session {
                Self::Docker(_, state) | Self::DockerSandbox(_, state) => {
                    state.runtime = Some(Arc::new(runtime))
                }
                Self::Ssh(_) => unreachable!(),
            }
            Ok(session)
        })();
        let confirmed = provider_retirement
            .as_ref()
            .is_none_or(|retirement| retirement.check().is_ok());
        cancel.finish(result, confirmed)
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

    pub fn python_available(&self) -> bool {
        // Prepared probes attest to runtime absence. R-backed SSH discovery
        // retains unresolved Python selection hints, not a negative capability.
        self.compute()
            .and_then(|state| state.runtime.as_ref())
            .and_then(|runtime| runtime.native.as_ref())
            .is_none_or(|runtime| runtime.python.is_some())
    }

    pub fn python_only(&self) -> bool {
        self.compute()
            .and_then(|state| state.runtime.as_ref())
            .and_then(|runtime| runtime.native.as_ref())
            .is_some_and(crate::local_runtime::Selection::python_only)
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
        let mut metadata = match self {
            Self::Ssh(session) => session.metadata(),
            Self::Docker(captured, _) => captured.metadata(),
            Self::DockerSandbox(captured, _) => captured.metadata(),
        };
        if let Some(runtime) = self.compute().and_then(|state| state.runtime.as_ref()) {
            metadata["runtime"] = serde_json::json!({
                "kind": if self.python_only() { "python" } else { "r" },
                "managed": false,
                "r_home": runtime.discovery.selections.r_home,
                "python": runtime.native.as_ref().and_then(|selection| selection.python.as_ref()).map(|python| &python.selected.embedding.python).or(runtime.discovery.selections.python.as_ref()),
            });
            if let Some(crate::local_runtime::Python { selected, .. }) = runtime
                .native
                .as_ref()
                .and_then(|runtime| runtime.python.as_ref())
            {
                // Recorded installation paths are target metadata only. Keep
                // the retained descriptor as the authoritative launch choice.
                for (name, path) in [
                    ("libpython", &selected.embedding.libpython),
                    ("prefix", &selected.prefix),
                    ("exec_prefix", &selected.exec_prefix),
                    ("base_prefix", &selected.base_prefix),
                    ("base_exec_prefix", &selected.base_exec_prefix),
                ] {
                    metadata["runtime"][name] = serde_json::Value::String(path.clone());
                }
            }
        }
        metadata
    }

    pub fn launch(
        &self,
        policy: &SandboxSettings,
        no_sandbox: bool,
        managed_r: Option<&crate::resolver::ManagedR>,
        python: Option<&crate::resolver::ManagedPython>,
        native: Option<&crate::local_runtime::Selection>,
    ) -> Result<(Command, Vec<u8>, Generation), String> {
        let (command, bytes, owner) = match self {
            Self::Ssh(session) => (
                session.command()?,
                session.bootstrap(policy, no_sandbox, managed_r, python, native)?,
                GenerationOwner::Ssh(Box::new(session.clone())),
            ),
            Self::Docker(captured, state) => state.launch(
                captured,
                &captured.target,
                policy,
                no_sandbox,
                self.provider(),
                ComputeLaunch::Worker,
            )?,
            Self::DockerSandbox(captured, state) => state.launch(
                captured,
                &captured.target,
                policy,
                no_sandbox,
                self.provider(),
                ComputeLaunch::Worker,
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
        operation: ComputeLaunch,
    ) -> Result<(Command, Vec<u8>, GenerationOwner), String> {
        let label = self.profile.protocol.0;
        if let Some(error) = &*self
            .blocked
            .lock()
            .map_err(|_| format!("{label} session lock poisoned"))?
        {
            return Err(error.clone());
        }
        let (probe, python) = match operation {
            ComputeLaunch::Probe(python) => (true, python),
            ComputeLaunch::Worker => (false, None),
        };
        let name = format!("mcp-console-{}", target_launch::owner::token()?);
        let request = target_launch::owner::Request {
            session: captured,
            name: name.clone(),
            probe,
            bootstrap: Bootstrap {
                languages: self.languages,
                version: target_launch::VERSION,
                build: env!("CARGO_PKG_VERSION").into(),
                workspace: target.workspace.clone(),
                policy: policy.clone(),
                writable_roots: self.roots.clone(),
                no_sandbox,
                provider,
                environment: self.runtime.as_deref().cloned(),
                python,
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
