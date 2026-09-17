#![cfg_attr(not(unix), allow(dead_code))]
//! Keep unsupported controller transports unavailable without Unix imports.
pub(crate) mod process {
    use std::time::{Duration, Instant};
    #[derive(Clone)]
    pub(crate) struct Cancel;
    impl Cancel {
        pub fn new(_: super::super::Protocol) -> Result<Self, String> {
            Err("Windows currently supports local execution only".into())
        }
        pub fn check(&self) -> Result<(), String> {
            Err("target execution is unavailable on this platform".into())
        }
    }
    impl crate::resolver::ResolverControl for Cancel {
        fn stop(&self) -> Result<(), String> {
            Ok(())
        }
        fn interrupt(&self) -> Result<bool, String> {
            Ok(false)
        }
        fn control_outcome(&self) -> Option<crate::resolver::ResolverControlOutcome> {
            None
        }
        fn cleanup_confirmed(&self) -> bool {
            true
        }
    }
    #[derive(Clone, Copy)]
    pub(crate) enum OutputMode {
        Capture,
        Diagnostics,
        Data,
    }
    pub(crate) struct OwnerInput {
        pub bytes: Vec<u8>,
        pub retirement_grace: Duration,
    }
    pub(crate) fn run(
        _: std::process::Command,
        _: &Cancel,
        _: Option<Instant>,
        _: OutputMode,
        _: Option<OwnerInput>,
    ) -> Result<Vec<u8>, String> {
        Err("target execution is unavailable on this platform".into())
    }
}
pub(crate) mod owner {
    #[derive(serde::Serialize)]
    pub(crate) struct Request<T> {
        pub session: T,
        pub name: String,
        pub probe: bool,
        pub bootstrap: super::super::Bootstrap,
    }
    pub(crate) fn token() -> Result<String, String> {
        Err("target execution is unavailable on this platform".into())
    }
}
