use std::collections::{HashMap, HashSet, VecDeque};
use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");
const PROCESS_EXIT_TIMEOUT: Duration = Duration::from_secs(5);
const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(10);
const TERMINATION_GRACE_PERIOD: Duration = Duration::from_secs(2);
const TRACKER_STOP_IDENT: libc::uintptr_t = 1;
const TRACKER_EVENT_CAPACITY: usize = 32;
const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

static SANDBOX_ROOT_PID: AtomicI32 = AtomicI32::new(0);
static SIGNAL_WINDOW_ACTIVE: AtomicBool = AtomicBool::new(false);
static TERMINATION_REQUESTED: AtomicBool = AtomicBool::new(false);
static FORCE_EXIT_REQUESTED: AtomicBool = AtomicBool::new(false);

pub(super) fn run(command: &[OsString]) -> Result<ExitCode, String> {
    let mut temp_directory = TemporaryDirectory::new()?;
    let mut signal_forwarder = SignalForwarder::install()?;
    let mut foreground_terminal = ForegroundTerminal::detect();

    let mut sandbox_command = Command::new(SANDBOX_EXEC);
    sandbox_command
        .arg("-p")
        .arg(POLICY)
        .arg(parameter_definition(
            "TEMP_DIRECTORY",
            temp_directory.path(),
        ))
        .arg("--")
        .args(command)
        .env("TMPDIR", temp_directory.path());
    signal_forwarder.restore_child_actions(&mut sandbox_command, foreground_terminal.descriptor());

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))?;
    signal_forwarder.set_root(child.id() as libc::pid_t);

    let tracker = match DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(error) => {
            let _ = kill_root(&mut child);
            signal_forwarder.clear_root();
            temp_directory.preserve();
            return Err(error);
        }
    };
    if let Err(error) = signal_forwarder.restore_original_mask() {
        let _ = kill_root(&mut child);
        signal_forwarder.clear_root();
        let _ = tracker.terminate();
        temp_directory.preserve();
        return Err(error);
    }

    let status_result = wait_for_root(&mut child, &mut signal_forwarder);
    let terminal_result = foreground_terminal.restore();
    let status = match status_result {
        Ok(status) => status,
        Err(error) => {
            let _ = tracker.terminate();
            temp_directory.preserve();
            return Err(error);
        }
    };

    if let Err(error) = tracker.terminate() {
        // Descendants may still be using their writable directory. Preserve it
        // when supervision fails instead of deleting files underneath them.
        temp_directory.preserve();
        return Err(error);
    }

    terminal_result?;
    Ok(exit_code(status))
}

struct ForegroundTerminal {
    descriptor: Option<libc::c_int>,
    launcher_process_group: libc::pid_t,
}

impl ForegroundTerminal {
    fn detect() -> Self {
        let launcher_process_group = unsafe { libc::getpgrp() };
        let descriptor = [libc::STDIN_FILENO, libc::STDOUT_FILENO, libc::STDERR_FILENO]
            .into_iter()
            .find(|descriptor| unsafe { libc::tcgetpgrp(*descriptor) } == launcher_process_group);

        Self {
            descriptor,
            launcher_process_group,
        }
    }

    fn descriptor(&self) -> Option<libc::c_int> {
        self.descriptor
    }

    fn restore(&mut self) -> Result<(), String> {
        let Some(descriptor) = self.descriptor else {
            return Ok(());
        };

        set_foreground_process_group(descriptor, self.launcher_process_group).map_err(|error| {
            format!("failed to restore the launcher as the foreground process group: {error}")
        })?;
        self.descriptor = None;
        Ok(())
    }
}

impl Drop for ForegroundTerminal {
    fn drop(&mut self) {
        let _ = self.restore();
    }
}

fn set_foreground_process_group(
    descriptor: libc::c_int,
    process_group: libc::pid_t,
) -> std::io::Result<()> {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut signal_set);
        libc::sigaddset(&mut signal_set, libc::SIGTTOU);
    }
    let mask_result =
        unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
    if mask_result != 0 {
        return Err(std::io::Error::from_raw_os_error(mask_result));
    }

    let terminal_result = unsafe { libc::tcsetpgrp(descriptor, process_group) };
    let terminal_error = std::io::Error::last_os_error();
    let mask_result =
        unsafe { libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut()) };

    if terminal_result != 0 {
        return Err(terminal_error);
    }
    if mask_result != 0 {
        return Err(std::io::Error::from_raw_os_error(mask_result));
    }
    Ok(())
}

struct SignalForwarder {
    previous_actions: Vec<(libc::c_int, libc::sigaction)>,
    previous_mask: libc::sigset_t,
    signals_blocked: bool,
}

impl SignalForwarder {
    fn install() -> Result<Self, String> {
        let signal_set = forwarded_signal_set();
        let mut previous_mask: libc::sigset_t = unsafe { std::mem::zeroed() };
        let mask_result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, &mut previous_mask) };
        if mask_result != 0 {
            return Err(format!(
                "failed to block sandbox-forwarded signals: {}",
                std::io::Error::from_raw_os_error(mask_result)
            ));
        }
        SIGNAL_WINDOW_ACTIVE.store(false, Ordering::SeqCst);
        TERMINATION_REQUESTED.store(false, Ordering::SeqCst);
        FORCE_EXIT_REQUESTED.store(false, Ordering::SeqCst);

        let mut previous_actions = Vec::with_capacity(FORWARDED_SIGNALS.len());
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = forward_signal as *const () as libc::sighandler_t;
        action.sa_flags = libc::SA_RESTART;
        unsafe { libc::sigemptyset(&mut action.sa_mask) };

        for signal in FORWARDED_SIGNALS {
            let mut previous: libc::sigaction = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigaction(signal, &action, &mut previous) } != 0 {
                let error = std::io::Error::last_os_error();
                for (installed_signal, installed_previous) in previous_actions.iter().rev() {
                    let _ = unsafe {
                        libc::sigaction(*installed_signal, installed_previous, std::ptr::null_mut())
                    };
                }
                let _ = unsafe {
                    libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut())
                };
                return Err(format!(
                    "failed to install sandbox signal forwarding for signal {signal}: {error}"
                ));
            }
            previous_actions.push((signal, previous));
        }

        Ok(Self {
            previous_actions,
            previous_mask,
            signals_blocked: true,
        })
    }

    fn restore_child_actions(
        &self,
        command: &mut Command,
        terminal_descriptor: Option<libc::c_int>,
    ) {
        let previous_actions: Vec<_> = self
            .previous_actions
            .iter()
            .map(|(signal, action)| (*signal, unsafe { std::ptr::read(action) }))
            .collect();
        let previous_mask = unsafe { std::ptr::read(&self.previous_mask) };

        unsafe {
            command.pre_exec(move || {
                // Give the command a dedicated process group. If this launcher
                // owns a terminal, hand foreground control to that group before
                // exec so terminal signals reach it directly and exactly once.
                // Stopped/continued job-control state is intentionally not
                // proxied; supporting Ctrl-Z requires a separate wait state
                // machine that restores and later reassigns the terminal.
                if libc::setpgid(0, 0) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
                if let Some(descriptor) = terminal_descriptor {
                    set_foreground_process_group(descriptor, libc::getpid())?;
                }
                for (signal, action) in &previous_actions {
                    if libc::sigaction(*signal, action, std::ptr::null_mut()) != 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                }
                let mask_result =
                    libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut());
                if mask_result != 0 {
                    return Err(std::io::Error::from_raw_os_error(mask_result));
                }
                Ok(())
            });
        }
    }

    fn set_root(&self, pid: libc::pid_t) {
        SANDBOX_ROOT_PID.store(pid, Ordering::SeqCst);
    }

    fn clear_root(&self) {
        SANDBOX_ROOT_PID.store(0, Ordering::SeqCst);
    }

    fn block_forwarded_signals(&mut self) -> Result<(), String> {
        if self.signals_blocked {
            return Ok(());
        }

        let signal_set = forwarded_signal_set();
        let result =
            unsafe { libc::pthread_sigmask(libc::SIG_BLOCK, &signal_set, std::ptr::null_mut()) };
        if result != 0 {
            return Err(format!(
                "failed to block sandbox-forwarded signals: {}",
                std::io::Error::from_raw_os_error(result)
            ));
        }
        self.signals_blocked = true;
        Ok(())
    }

    fn restore_original_mask(&mut self) -> Result<(), String> {
        if !self.signals_blocked {
            return Ok(());
        }

        let result = unsafe {
            libc::pthread_sigmask(libc::SIG_SETMASK, &self.previous_mask, std::ptr::null_mut())
        };
        if result != 0 {
            return Err(format!(
                "failed to restore the launcher signal mask: {}",
                std::io::Error::from_raw_os_error(result)
            ));
        }
        self.signals_blocked = false;
        Ok(())
    }
}

impl Drop for SignalForwarder {
    fn drop(&mut self) {
        self.clear_root();
        let _ = self.block_forwarded_signals();
        for (signal, previous) in self.previous_actions.iter().rev() {
            let _ = unsafe { libc::sigaction(*signal, previous, std::ptr::null_mut()) };
        }
        let _ = self.restore_original_mask();
    }
}

fn forwarded_signal_set() -> libc::sigset_t {
    let mut signal_set: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe { libc::sigemptyset(&mut signal_set) };
    for signal in FORWARDED_SIGNALS {
        unsafe { libc::sigaddset(&mut signal_set, signal) };
    }
    signal_set
}

fn wait_for_root(
    child: &mut std::process::Child,
    signal_forwarder: &mut SignalForwarder,
) -> Result<ExitStatus, String> {
    let mut termination_deadline = None;
    let mut repeat_deadline = None;
    loop {
        if let Err(error) = signal_forwarder.block_forwarded_signals() {
            let _ = kill_root(child);
            signal_forwarder.clear_root();
            return Err(error);
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                signal_forwarder.clear_root();
                signal_forwarder.restore_original_mask()?;
                return Ok(status);
            }
            Ok(None) => {
                let now = Instant::now();
                if repeat_deadline.is_some_and(|deadline| now >= deadline) {
                    SIGNAL_WINDOW_ACTIVE.store(false, Ordering::SeqCst);
                    repeat_deadline = None;
                } else if repeat_deadline.is_none() && SIGNAL_WINDOW_ACTIVE.load(Ordering::SeqCst) {
                    repeat_deadline = Some(now + TERMINATION_GRACE_PERIOD);
                }
                if TERMINATION_REQUESTED.load(Ordering::SeqCst) {
                    termination_deadline.get_or_insert_with(|| now + TERMINATION_GRACE_PERIOD);
                }
                let force_exit = FORCE_EXIT_REQUESTED.load(Ordering::SeqCst)
                    || termination_deadline.is_some_and(|deadline| now >= deadline);
                if force_exit {
                    let status = kill_root(child)?;
                    signal_forwarder.clear_root();
                    signal_forwarder.restore_original_mask()?;
                    return Ok(status);
                }
                if let Err(error) = signal_forwarder.restore_original_mask() {
                    let _ = kill_root(child);
                    signal_forwarder.clear_root();
                    return Err(error);
                }
                thread::sleep(PROCESS_POLL_INTERVAL);
            }
            Err(error) => {
                let _ = kill_root(child);
                signal_forwarder.clear_root();
                let _ = signal_forwarder.restore_original_mask();
                return Err(format!("failed to wait for `{SANDBOX_EXEC}`: {error}"));
            }
        }
    }
}

fn kill_root(child: &mut std::process::Child) -> Result<ExitStatus, String> {
    let result = unsafe { libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL) };
    if result != 0 {
        let kill_error = std::io::Error::last_os_error();
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                return Err(format!(
                    "failed to terminate the `{SANDBOX_EXEC}` process group: {kill_error}"
                ));
            }
            Err(wait_error) => {
                return Err(format!(
                    "failed to terminate the `{SANDBOX_EXEC}` process group: \
                     {kill_error}; additionally failed to read its status: {wait_error}"
                ));
            }
        }
    }
    child
        .wait()
        .map_err(|error| format!("failed to wait for terminated `{SANDBOX_EXEC}`: {error}"))
}

extern "C" fn forward_signal(signal: libc::c_int) {
    let saved_errno = unsafe { *libc::__error() };
    let repeated = SIGNAL_WINDOW_ACTIVE.swap(true, Ordering::SeqCst);
    if repeated {
        // A second request is the escape hatch for an interactive command that
        // handled the first SIGINT without exiting.
        FORCE_EXIT_REQUESTED.store(true, Ordering::SeqCst);
    }
    if signal != libc::SIGINT {
        TERMINATION_REQUESTED.store(true, Ordering::SeqCst);
    }

    let root_pid = SANDBOX_ROOT_PID.load(Ordering::Relaxed);
    if root_pid > 0 && !repeated {
        // kill(2) is async-signal-safe. The supervisor remains alive to finish
        // tracking and terminating descendants after the root handles the signal.
        let _ = unsafe { libc::kill(-root_pid, signal) };
    }
    unsafe { *libc::__error() = saved_errno };
}

#[derive(Clone, Copy, Eq, Hash, PartialEq)]
struct ProcessIdentity {
    pid: libc::pid_t,
    started_seconds: u64,
    started_microseconds: u64,
}

struct DescendantTracker {
    kqueue: libc::c_int,
    stop_requested: Arc<AtomicBool>,
    handle: JoinHandle<Result<(), String>>,
}

impl DescendantTracker {
    fn start(root_pid: libc::pid_t) -> Result<Self, String> {
        let kqueue = unsafe { libc::kqueue() };
        if kqueue < 0 {
            return Err(format!(
                "failed to create the sandbox process tracker: {}",
                std::io::Error::last_os_error()
            ));
        }

        if let Err(error) = register_stop_event(kqueue) {
            let _ = unsafe { libc::close(kqueue) };
            return Err(error);
        }

        // Darwin provides neither child subreapers nor PID namespaces, and its
        // kqueue NOTE_TRACK facility is unsupported. A descendant that becomes
        // orphaned before this post-spawn watch, or before a NOTE_FORK event can
        // be paired with a libproc snapshot, is therefore an intentional boundary
        // of the initial launcher. Once observed, descendants that call setsid(),
        // such as processx children, remain tracked by their PID and start time.
        let mut state = TrackerState {
            root: process_identity(root_pid),
            active: HashMap::new(),
        };
        if let Err(error) = add_process_tree(kqueue, root_pid, None, &mut state) {
            let _ = unsafe { libc::close(kqueue) };
            return Err(error);
        }

        let stop_requested = Arc::new(AtomicBool::new(false));
        let tracker_stop_requested = Arc::clone(&stop_requested);
        let handle = match thread::Builder::new()
            .name("mcp-console-process-tracker".to_string())
            .spawn(move || track_descendants(kqueue, state, tracker_stop_requested))
        {
            Ok(handle) => handle,
            Err(error) => {
                let _ = unsafe { libc::close(kqueue) };
                return Err(format!(
                    "failed to start the sandbox process tracker: {error}"
                ));
            }
        };

        Ok(Self {
            kqueue,
            stop_requested,
            handle,
        })
    }

    fn terminate(self) -> Result<(), String> {
        self.stop_requested.store(true, Ordering::Release);
        // The user event normally wakes kevent immediately. The shared flag and
        // bounded idle timeout remain the fallback if the wake-up syscall fails.
        let _ = trigger_stop_event(self.kqueue);
        let tracker_result = self
            .handle
            .join()
            .map_err(|_| "sandbox process tracker panicked".to_string())
            .and_then(|result| result);
        let _ = unsafe { libc::close(self.kqueue) };

        tracker_result
    }
}

struct TrackerState {
    root: Option<ProcessIdentity>,
    active: HashMap<libc::pid_t, ProcessIdentity>,
}

fn track_descendants(
    kqueue: libc::c_int,
    mut state: TrackerState,
    stop_requested: Arc<AtomicBool>,
) -> Result<(), String> {
    let mut events: [libc::kevent; TRACKER_EVENT_CAPACITY] =
        unsafe { std::mem::MaybeUninit::zeroed().assume_init() };
    let mut deadline = None;

    loop {
        if stop_requested.load(Ordering::Acquire) {
            deadline.get_or_insert_with(|| Instant::now() + PROCESS_EXIT_TIMEOUT);
        }
        let timeout_duration = if deadline.is_some() {
            PROCESS_POLL_INTERVAL
        } else {
            Duration::from_secs(1)
        };
        let timeout = libc::timespec {
            tv_sec: timeout_duration.as_secs() as libc::time_t,
            tv_nsec: timeout_duration.subsec_nanos() as libc::c_long,
        };
        let event_count = unsafe {
            libc::kevent(
                kqueue,
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as libc::c_int,
                &timeout,
            )
        };
        if event_count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("sandbox process tracker failed: {error}"));
        }

        for event in events.iter().take(event_count as usize) {
            if event.filter == libc::EVFILT_USER && event.ident == TRACKER_STOP_IDENT {
                deadline.get_or_insert_with(|| Instant::now() + PROCESS_EXIT_TIMEOUT);
                continue;
            }

            if event.filter != libc::EVFILT_PROC {
                continue;
            }

            let pid = event.ident as libc::pid_t;
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event_data == libc::ESRCH as libc::intptr_t {
                    state.active.remove(&pid);
                    continue;
                }
                return Err(format!(
                    "sandbox process tracker received error {} for process {pid}",
                    event_data
                ));
            }

            if event.fflags & libc::NOTE_FORK != 0 {
                add_children(kqueue, pid, &mut state)?;
            }
            if event.fflags & libc::NOTE_EXIT != 0 {
                state.active.remove(&pid);
            }
        }

        if stop_requested.load(Ordering::Acquire) {
            deadline.get_or_insert_with(|| Instant::now() + PROCESS_EXIT_TIMEOUT);
        }
        let Some(deadline) = deadline else {
            continue;
        };

        // Re-snapshot before each signal pass to narrow the teardown fork window.
        // A child that becomes orphaned before observation remains outside the
        // documented initial supervision boundary.
        discover_active_children(kqueue, &mut state)?;
        if let Some(root) = state.root {
            if state.active.get(&root.pid) == Some(&root) {
                state.active.remove(&root.pid);
            }
        }
        state
            .active
            .retain(|_, identity| process_identity(identity.pid) == Some(*identity));

        // The root command has exited, so its background work has no remaining
        // lifetime to preserve. SIGKILL keeps teardown bounded and deterministic.
        for identity in state.active.values().copied() {
            signal_process(identity, libc::SIGKILL)?;
        }
        state
            .active
            .retain(|_, identity| process_identity(identity.pid) == Some(*identity));

        if state.active.is_empty() {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("timed out waiting for sandbox descendants to exit".to_string());
        }
    }
}

fn register_stop_event(kqueue: libc::c_int) -> Result<(), String> {
    let event = libc::kevent {
        ident: TRACKER_STOP_IDENT,
        filter: libc::EVFILT_USER,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    submit_event(kqueue, &event, "register the process tracker stop event")
}

fn trigger_stop_event(kqueue: libc::c_int) -> Result<(), String> {
    let event = libc::kevent {
        ident: TRACKER_STOP_IDENT,
        filter: libc::EVFILT_USER,
        flags: 0,
        fflags: libc::NOTE_TRIGGER,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    submit_event(kqueue, &event, "stop the sandbox process tracker")
}

fn submit_event(kqueue: libc::c_int, event: &libc::kevent, action: &str) -> Result<(), String> {
    loop {
        let result =
            unsafe { libc::kevent(kqueue, event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
        if result >= 0 {
            return Ok(());
        }

        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to {action}: {error}"));
        }
    }
}

fn add_process_tree(
    kqueue: libc::c_int,
    root_pid: libc::pid_t,
    expected_parent: Option<ProcessIdentity>,
    state: &mut TrackerState,
) -> Result<(), String> {
    let mut queue = VecDeque::new();
    queue.push_back((root_pid, expected_parent));
    let mut visited = HashSet::new();

    while let Some((pid, expected_parent)) = queue.pop_front() {
        let Some(info) = process_info(pid) else {
            continue;
        };
        if let Some(parent) = expected_parent {
            // proc_listchildpids() returns numeric PIDs. Confirm both the
            // parent relationship and parent start time before adopting one,
            // so PID reuse cannot turn an unrelated process into a target.
            if info.parent_pid != parent.pid || process_identity(parent.pid) != Some(parent) {
                continue;
            }
        }
        let identity = info.identity;
        if !visited.insert(identity) {
            continue;
        }

        if state.active.get(&pid) != Some(&identity) {
            state.active.remove(&pid);
            match watch_process(kqueue, pid) {
                Ok(()) => {}
                Err(WatchProcessError::Gone) => continue,
                Err(WatchProcessError::Other(error)) => {
                    return Err(format!("failed to watch sandbox process {pid}: {error}"));
                }
            }

            // Confirm the watch still names the process whose start time was
            // recorded; PID reuse must not turn an old descendant into a new target.
            if process_identity(pid) != Some(identity) {
                remove_process_watch(kqueue, pid);
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

fn add_children(
    kqueue: libc::c_int,
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

fn discover_active_children(kqueue: libc::c_int, state: &mut TrackerState) -> Result<(), String> {
    let parents: Vec<_> = state.active.values().copied().collect();
    for parent in parents {
        if process_identity(parent.pid) == Some(parent) {
            add_children(kqueue, parent.pid, state)?;
        }
    }
    Ok(())
}

enum WatchProcessError {
    Gone,
    Other(std::io::Error),
}

fn watch_process(kqueue: libc::c_int, pid: libc::pid_t) -> Result<(), WatchProcessError> {
    let event = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: libc::NOTE_FORK | libc::NOTE_EXEC | libc::NOTE_EXIT,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    let result =
        unsafe { libc::kevent(kqueue, &event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
    if result >= 0 {
        return Ok(());
    }

    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Err(WatchProcessError::Gone)
    } else {
        Err(WatchProcessError::Other(error))
    }
}

fn remove_process_watch(kqueue: libc::c_int, pid: libc::pid_t) {
    let event = libc::kevent {
        ident: pid as libc::uintptr_t,
        filter: libc::EVFILT_PROC,
        flags: libc::EV_DELETE,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    let _ = unsafe { libc::kevent(kqueue, &event, 1, std::ptr::null_mut(), 0, std::ptr::null()) };
}

fn list_child_pids(parent: libc::pid_t) -> Result<Vec<libc::pid_t>, String> {
    let mut capacity = 16;
    loop {
        let mut children = vec![0; capacity];
        let count = unsafe {
            libc::proc_listchildpids(
                parent,
                children.as_mut_ptr().cast(),
                std::mem::size_of_val(children.as_slice()) as libc::c_int,
            )
        };
        if count == 0 {
            return Ok(Vec::new());
        }
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.raw_os_error() == Some(libc::ESRCH) {
                return Ok(Vec::new());
            }
            return Err(format!(
                "failed to list children of sandbox process {parent}: {error}"
            ));
        }

        let count = count as usize;
        if count < capacity {
            children.truncate(count);
            return Ok(children);
        }
        capacity = capacity.saturating_mul(2).max(count + 16);
    }
}

struct ProcessInfo {
    identity: ProcessIdentity,
    parent_pid: libc::pid_t,
}

fn process_info(pid: libc::pid_t) -> Option<ProcessInfo> {
    if pid <= 0 {
        return None;
    }

    let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
    let size = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            info.as_mut_ptr().cast(),
            std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int,
        )
    };
    if size as usize != std::mem::size_of::<libc::proc_bsdinfo>() {
        return None;
    }

    let info = unsafe { info.assume_init() };
    Some(ProcessInfo {
        identity: ProcessIdentity {
            pid,
            started_seconds: info.pbi_start_tvsec,
            started_microseconds: info.pbi_start_tvusec,
        },
        parent_pid: info.pbi_ppid as libc::pid_t,
    })
}

fn process_identity(pid: libc::pid_t) -> Option<ProcessIdentity> {
    process_info(pid).map(|info| info.identity)
}

fn signal_process(identity: ProcessIdentity, signal: libc::c_int) -> Result<bool, String> {
    if process_identity(identity.pid) != Some(identity) {
        return Ok(false);
    }

    // macOS has no pidfd-like signal API, so PID reuse remains possible in the
    // narrow interval between this identity check and kill().
    let result = unsafe { libc::kill(identity.pid, signal) };
    if result == 0 {
        return Ok(true);
    }

    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(false)
    } else {
        Err(format!(
            "failed to signal sandbox descendant {}: {error}",
            identity.pid
        ))
    }
}

struct TemporaryDirectory {
    path: PathBuf,
    remove_on_drop: bool,
}

impl TemporaryDirectory {
    fn new() -> Result<Self, String> {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| format!("failed to read the system clock: {error}"))?
            .as_nanos();
        let path =
            std::env::temp_dir().join(format!("mcp-console-tmp-{}-{unique}", std::process::id()));

        DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .map_err(|error| {
                format!(
                    "failed to create temporary directory `{}`: {error}",
                    path.display()
                )
            })?;

        let path = path.canonicalize().map_err(|error| {
            format!(
                "failed to resolve temporary directory `{}`: {error}",
                path.display()
            )
        })?;
        Ok(Self {
            path,
            remove_on_drop: true,
        })
    }

    fn path(&self) -> &Path {
        &self.path
    }

    fn preserve(&mut self) {
        self.remove_on_drop = false;
    }
}

impl Drop for TemporaryDirectory {
    fn drop(&mut self) {
        // Cleanup must not replace the child status if it changed directory modes.
        if self.remove_on_drop {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}

// `sandbox-exec -DNAME=VALUE` supplies values for `(param "NAME")` in the SBPL.
fn parameter_definition(name: &str, path: &Path) -> OsString {
    let mut argument = OsString::from("-D");
    argument.push(name);
    argument.push("=");
    argument.push(path);
    argument
}

fn exit_code(status: ExitStatus) -> ExitCode {
    let code = status
        .code()
        .unwrap_or_else(|| 128 + status.signal().unwrap_or(1));
    ExitCode::from(code as u8)
}
