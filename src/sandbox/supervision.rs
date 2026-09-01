#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/manager.rs"]
mod manager;
#[path = "supervision/process.rs"]
mod process;
#[path = "supervision/process_retirement.rs"]
mod process_retirement;
#[path = "supervision/process_tracker.rs"]
mod process_tracker;
#[path = "supervision/process_tree.rs"]
mod process_tree;
#[path = "supervision/root_exit_waiter.rs"]
mod root_exit_waiter;
#[path = "supervision/standalone.rs"]
mod standalone;

pub(crate) use self::manager::SandboxManager;
pub(super) use self::standalone::status;
use super::file_descriptors::configure as configure_file_descriptors;
use std::os::fd::RawFd;
use std::process::Command;

pub(super) fn configure_command(
    command: &mut Command,
    inherited_descriptors: Vec<RawFd>,
) -> Result<(), String> {
    configure_file_descriptors(command, inherited_descriptors)
}

pub(super) fn run_manager() -> Result<(), String> {
    manager::run()
}
