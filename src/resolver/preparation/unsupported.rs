use super::{Discovery, Operation, Selections};
use crate::resolver::ResolverStopHandle;

#[derive(Clone)]
pub(crate) struct Preparation;

impl Preparation {
    pub(crate) fn open(
        _session: &crate::ssh::Session,
        _selections: Selections,
        _no_sandbox: bool,
        _settings: crate::resolver::broker::Settings,
        _on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        Err("resolver preparation requires macOS or Linux".into())
    }

    pub(crate) fn local(
        _launch: crate::resolver::broker::Launch,
        _on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        Err("resolver preparation requires macOS or Linux".into())
    }

    pub(crate) fn call<T>(
        &self,
        _operation: Operation,
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String> {
        Err("resolver preparation requires macOS or Linux".into())
    }

    pub(crate) fn close(&self) -> Result<(), String> {
        Ok(())
    }
}
