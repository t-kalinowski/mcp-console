use super::kqueue::{Kqueue, ProcessWatchError};
use super::process::{ProcessIdentity, list_child_pids, process_identity, process_info};
use std::collections::{HashMap, HashSet, VecDeque};

#[allow(deprecated)]
pub(super) const PROCESS_REAP_EVENT: u32 = libc::NOTE_REAP;

pub(super) struct TrackerState {
    pub(super) root: Option<ProcessIdentity>,
    pub(super) active: HashMap<libc::pid_t, ProcessIdentity>,
}

pub(super) fn remove_stale_processes(
    active: &mut HashMap<libc::pid_t, ProcessIdentity>,
) -> Result<(), String> {
    let identities: Vec<_> = active.values().copied().collect();
    let mut error = None;
    for identity in identities {
        match process_identity(identity.pid) {
            Ok(Some(current)) if current == identity => {}
            Ok(Some(_) | None) => {
                active.remove(&identity.pid);
            }
            Err(process_error) => record_first_error(&mut error, process_error),
        }
    }
    error.map_or(Ok(()), Err)
}

pub(super) fn record_first_error(current: &mut Option<String>, error: String) {
    if current.is_none() {
        *current = Some(error);
    }
}

pub(super) fn add_process_tree(
    kqueue: &Kqueue,
    root_pid: libc::pid_t,
    expected_parent: Option<ProcessIdentity>,
    state: &mut TrackerState,
) -> Result<(), String> {
    let mut queue = VecDeque::new();
    queue.push_back((root_pid, expected_parent));
    let mut visited = HashSet::new();

    while let Some((pid, expected_parent)) = queue.pop_front() {
        let Some(info) = process_info(pid)? else {
            continue;
        };
        if let Some(parent) = expected_parent {
            // proc_listchildpids() returns numeric PIDs. Confirm both the
            // parent relationship and parent start time before adopting one,
            // so PID reuse cannot turn an unrelated process into a target.
            if info.parent_pid != parent.pid || process_identity(parent.pid)? != Some(parent) {
                continue;
            }
        }
        let identity = info.identity;
        if !visited.insert(identity) {
            continue;
        }

        if info.is_zombie {
            // macOS cannot attach a reliable process watch after exit, but the
            // tracker can still retain this verified identity until live
            // observation or retirement classifies it.
            state.active.insert(pid, identity);
            continue;
        }

        if state.active.get(&pid) != Some(&identity) {
            state.active.remove(&pid);
            // NOTE_REAP is deprecated for ordinary exit observation, but
            // NOTE_EXIT fires while the PID may still exist as a zombie.
            // Requesting both keeps descendant watches until processes are reaped.
            match kqueue.watch_process(pid, libc::NOTE_FORK | libc::NOTE_EXIT | PROCESS_REAP_EVENT)
            {
                Ok(()) => {}
                Err(ProcessWatchError::Gone) => {
                    if process_info(pid)?
                        .is_some_and(|current| current.identity == identity && current.is_zombie)
                    {
                        state.active.insert(pid, identity);
                    }
                    continue;
                }
                Err(ProcessWatchError::Other(error)) => {
                    return Err(format!("failed to watch sandbox process {pid}: {error}"));
                }
            }

            // Confirm the watch still names the process whose start time was
            // recorded; PID reuse must not turn an old descendant into a new target.
            if process_identity(pid)? != Some(identity) {
                kqueue.remove_process_watch(pid);
                continue;
            }
            state.active.insert(pid, identity);
        }

        queue.extend(
            list_child_pids(pid)?
                .into_iter()
                .map(|child| (child, Some(identity))),
        );
    }

    Ok(())
}

pub(super) fn add_children(
    kqueue: &Kqueue,
    parent: libc::pid_t,
    state: &mut TrackerState,
) -> Result<(), String> {
    let Some(parent_identity) = state.active.get(&parent).copied() else {
        return Ok(());
    };
    for child in list_child_pids(parent)? {
        add_process_tree(kqueue, child, Some(parent_identity), state)?;
    }
    Ok(())
}

pub(super) fn discover_active_children(
    kqueue: &Kqueue,
    state: &mut TrackerState,
) -> Result<(), String> {
    let parents: Vec<_> = state.active.values().copied().collect();
    for parent in parents {
        let Some(info) = process_info(parent.pid)? else {
            continue;
        };
        if info.identity == parent && !info.is_zombie {
            add_children(kqueue, parent.pid, state)?;
        }
    }
    Ok(())
}
