#[path = "supervision/job_control.rs"]
mod job_control;
#[path = "supervision/kqueue.rs"]
mod kqueue;
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

pub(super) use self::root_exit_waiter::SandboxOwner;
pub(super) use self::standalone::status;

pub(super) fn run_manager(
    root_pid: u32,
    cleanup_timeout_millis: u64,
    temporary_directory: std::path::PathBuf,
) -> Result<(), String> {
    manager::run(root_pid, cleanup_timeout_millis, temporary_directory)
}
