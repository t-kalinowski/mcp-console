//! The two implemented local compute targets; no controller runtime discovery.
use crate::settings::{Compute, SandboxSettings, Target};
use crate::target_launch::{Protocol, Retirement};
use std::path::PathBuf;
use std::process::Command;

#[derive(Clone)]
pub(crate) enum Session {
    Docker(crate::docker::Session),
    DockerSandbox(crate::docker_sandbox::Session),
}

impl Session {
    pub fn provider(&self) -> crate::settings::Provider {
        match self {
            Self::Docker(_) => crate::settings::Provider::Native,
            Self::DockerSandbox(_) => crate::settings::Provider::Compute,
        }
    }
    pub fn setup(
        target: Target,
        roots: Vec<PathBuf>,
        policy: &SandboxSettings,
        no_sandbox: bool,
        started: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<Self, String> {
        match target.compute {
            Compute::Docker(_) => {
                crate::docker::Session::setup(target, roots, policy, no_sandbox, started)
                    .map(Self::Docker)
            }
            Compute::DockerSandbox(_) => {
                crate::docker_sandbox::Session::setup(target, policy, no_sandbox, started)
                    .map(Self::DockerSandbox)
            }
            Compute::Host {} => unreachable!("host compute has its own runtime discovery"),
        }
    }
    pub fn retirement_grace(&self) -> std::time::Duration {
        std::time::Duration::from_secs(match self {
            Self::Docker(_) => 6,
            Self::DockerSandbox(_) => 20,
        })
    }
    pub fn protocol(&self) -> Protocol {
        Protocol(match self {
            Self::Docker(_) => "Docker",
            Self::DockerSandbox(_) => "Docker Sandbox",
        })
    }
    pub fn metadata(&self) -> serde_json::Value {
        match self {
            Self::Docker(session) => session.metadata(),
            Self::DockerSandbox(session) => session.metadata(),
        }
    }
    pub fn launch(
        &self,
        policy: &SandboxSettings,
        no_sandbox: bool,
        probe: bool,
    ) -> Result<(Command, Vec<u8>, String), String> {
        match self {
            Self::Docker(session) => session.launch(policy, no_sandbox, probe),
            Self::DockerSandbox(session) => session.launch(policy, no_sandbox, probe),
        }
    }
    pub fn check_retirement(&self, retirement: &Retirement, name: &str) -> Result<(), String> {
        match self {
            Self::Docker(session) => session.check_retirement(retirement, name),
            Self::DockerSandbox(session) => session.check_retirement(retirement, name),
        }
    }
}
