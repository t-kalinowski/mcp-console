//! Disposable response observations of the existing operation owners.

use std::sync::Weak;

use super::lifecycle::{LifecycleState, WorkerGeneration, WorkerStartupAdmission};
use super::{Client, ClientInner};

pub(super) struct Source {
    client: Weak<ClientInner>,
    generation: WorkerGeneration,
    owner: Owner,
}

enum Owner {
    Evaluation(Weak<super::Evaluation>),
    Resolver(crate::resolver::ResolverPhase),
    Startup(Weak<WorkerStartupAdmission>),
    ConnectionStartup,
    Replacement,
}

impl Source {
    pub(super) fn phase(&self) -> Option<&'static str> {
        let client = self.client.upgrade()?;
        let replacing = match &self.owner {
            Owner::Evaluation(evaluation) => evaluation.upgrade()?.replacement_observation()?,
            _ => false,
        };
        let lifecycle = client.lifecycle.lock().ok()?;
        if !lifecycle.generation.is(&self.generation)
            || matches!(lifecycle.state, LifecycleState::ShuttingDown { .. })
        {
            return None;
        }
        match &self.owner {
            Owner::Resolver(resolver) => resolver.phase(),
            Owner::Startup(startup) if startup.strong_count() != 0 => Some("startup"),
            Owner::ConnectionStartup
                if client.startup.borrow().is_none()
                    && self.generation.is(&client.startup_generation) =>
            {
                Some("startup")
            }
            Owner::Replacement if matches!(lifecycle.state, LifecycleState::Restarting { .. }) => {
                Some("replacement")
            }
            Owner::Evaluation(_) => {
                if replacing {
                    Some("replacement")
                } else if client.startup.borrow().is_none()
                    && self.generation.is(&client.startup_generation)
                {
                    Some("startup")
                } else if let Some(phase) = lifecycle
                    .processes
                    .resolver
                    .as_ref()
                    .and_then(|handle| handle.phase_observation().phase())
                {
                    Some(phase)
                } else if lifecycle.starting() {
                    Some("startup")
                } else {
                    None
                }
            }
            _ => None,
        }
    }
}

impl Client {
    pub(super) fn status_generation(&self) -> Option<WorkerGeneration> {
        Some(self.0.lifecycle.lock().ok()?.generation.clone())
    }

    pub(super) fn status_source(&self, generation: Option<WorkerGeneration>) -> Option<Source> {
        let generation = generation?;
        let evaluation = self
            .0
            .evaluation
            .lock()
            .ok()?
            .as_ref()
            .filter(|active| active.generation.is(&generation))
            .map(|active| std::sync::Arc::downgrade(&active.evaluation));
        let lifecycle = self.0.lifecycle.lock().ok()?;
        if !lifecycle.generation.is(&generation) {
            return None;
        }
        let owner = if let Some(evaluation) = evaluation {
            Owner::Evaluation(evaluation)
        } else if let Some(resolver) = lifecycle.processes.resolver.as_ref() {
            Owner::Resolver(resolver.phase_observation())
        } else if let Some(startup) = lifecycle.startup_observation() {
            Owner::Startup(startup)
        } else if self.0.startup.borrow().is_none() && generation.is(&self.0.startup_generation) {
            Owner::ConnectionStartup
        } else if matches!(lifecycle.state, LifecycleState::Restarting { .. }) {
            Owner::Replacement
        } else {
            return None;
        };
        Some(Source {
            client: std::sync::Arc::downgrade(&self.0),
            generation,
            owner,
        })
    }

    pub(crate) fn starting_response(&self) -> super::Response {
        let mut response = super::output::render_response(
            super::output::SendResponse::ReplacementStarting(super::Response::default()),
        );
        response.observe_phase(self.status_source(self.status_generation()));
        response
    }
}
