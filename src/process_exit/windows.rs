use std::os::windows::io::{AsRawHandle, OwnedHandle};
use std::time::Duration;

pub(crate) struct ChildExitWaiter {
    handle: OwnedHandle,
}
impl ChildExitWaiter {
    pub(crate) fn start(pid: u32) -> Result<Self, String> {
        Ok(Self {
            handle: crate::windows::process_handle(pid).map_err(|e| e.to_string())?,
        })
    }
    pub(crate) fn start_notifying(
        pid: u32,
        notify: impl FnOnce() + Send + 'static,
    ) -> Result<Self, String> {
        let waiter = Self::start(pid)?;
        let handle = waiter.handle.try_clone().map_err(|e| e.to_string())?;
        std::thread::Builder::new()
            .name("worker launcher exit".into())
            .spawn(move || {
                let _ = crate::windows::wait(handle.as_raw_handle(), None);
                notify();
            })
            .map_err(|e| e.to_string())?;
        Ok(waiter)
    }
    pub(crate) fn wait(&mut self, timeout: Duration) -> Result<bool, String> {
        crate::windows::wait(self.handle.as_raw_handle(), Some(timeout)).map_err(|e| e.to_string())
    }
}
