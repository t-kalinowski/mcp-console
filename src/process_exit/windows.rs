use std::os::windows::io::{AsRawHandle, OwnedHandle};

pub(super) struct Observer(OwnedHandle);

impl Observer {
    pub(super) fn new(process_id: u32) -> Result<Self, String> {
        crate::windows::process_handle(process_id)
            .map(Self)
            .map_err(|error| error.to_string())
    }

    pub(super) fn wait(self) -> Result<(), String> {
        crate::windows::wait(self.0.as_raw_handle(), None)
            .map(|_| ())
            .map_err(|error| error.to_string())
    }
}
