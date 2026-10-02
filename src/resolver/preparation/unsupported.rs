use super::{Discovery, Operation, Selections};
use crate::resolver::ResolverStopHandle;

#[derive(Clone)]
pub(crate) struct Preparation;

impl Preparation {
    pub(crate) fn open_local(
        _: super::Mode,
        _: &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, super::Discovery), String> {
        Err("managed dependency resolution is not yet supported on Windows; install packages before starting Console".into())
    }

    pub(crate) fn open(
        _session: &crate::ssh::Session,
        _selections: Selections,
        _on_started: &dyn Fn(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<(Self, Discovery), String> {
        Err("SSH preparation requires macOS or Linux".into())
    }

    pub(crate) fn call<T>(
        &self,
        _operation: Operation,
        _on_started: impl FnOnce(ResolverStopHandle) -> Result<(), String>,
    ) -> Result<T, String> {
        Err("SSH preparation requires macOS or Linux".into())
    }

    pub(crate) fn close(&self) -> Result<(), String> {
        Ok(())
    }
}
