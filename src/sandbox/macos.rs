use std::collections::{HashMap, HashSet, VecDeque};
use std::ffi::OsString;
use std::fs::{self, DirBuilder};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, ExitStatus};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
const POLICY: &str = include_str!("read_only_policy.sbpl");
const PROCESS_EXIT_TIMEOUT: Duration = Duration::from_secs(5);
const PROCESS_POLL_INTERVAL: Duration = Duration::from_millis(10);
const TRACKER_EVENT_CAPACITY: usize = 32;
const FORWARDED_SIGNALS: [libc::c_int; 4] =
    [libc::SIGHUP, libc::SIGINT, libc::SIGQUIT, libc::SIGTERM];

pub(super) fn run(command: &[OsString]) -> Result<ExitCode, String> {
    let mut temp_directory = TemporaryDirectory::new()?;
    let signal_relay = SignalRelay::install()?;
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
    signal_relay.configure_child(&mut sandbox_command, foreground_terminal.descriptor());

    let mut child = sandbox_command
        .spawn()
        .map_err(|error| format!("failed to launch `{SANDBOX_EXEC}`: {error}"))?;

    let mut tracker = match DescendantTracker::start(child.id() as libc::pid_t) {
        Ok(tracker) => tracker,
        Err(error) => {
            let _ = kill_root(&mut child);
            temp_directory.preserve();
            return Err(error);
        }
    };

    let status_result = wait_for_root(&mut child, &signal_relay, &mut tracker);
    let terminal_result = foreground_terminal.restore();
    let status = match status_result {
        Ok(status) => status,
        Err(error) => {
            let tracker_error = tracker.terminate().err();
            temp_directory.preserve();
            return Err(tracker_error.unwrap_or(error));
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

        if let Err(error) = set_foreground_process_group(descriptor, self.launcher_process_group) {
            if error.raw_os_error() != Some(libc::ENOTTY) {
                return Err(format!(
                    "failed to restore the launcher as the foreground process group: {error}"
                ));
            }
            // A revoked or hung-up controlling terminal no longer has foreground
            // ownership to restore. Preserve the command's actual exit status.
        }
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

struct SignalRelay {
    wait_set: libc::sigset_t,
    previous_mask: libc::sigset_t,
}

impl SignalRelay {
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

        // Preserve inherited masks and ignored dispositions. Previously blocked
        // signals remain pending, while Darwin discards ignored signals. This
        // one-shot launcher keeps its new mask until it exits; the child restores
        // the inherited mask before exec.
        let mut wait_set: libc::sigset_t = unsafe { std::mem::zeroed() };
        unsafe { libc::sigemptyset(&mut wait_set) };
        for signal in FORWARDED_SIGNALS {
            if unsafe { libc::sigismember(&previous_mask, signal) } == 0 {
                unsafe { libc::sigaddset(&mut wait_set, signal) };
            }
        }

        Ok(Self {
            wait_set,
            previous_mask,
        })
    }

    fn configure_child(&self, command: &mut Command, terminal_descriptor: Option<libc::c_int>) {
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
                let mask_result =
                    libc::pthread_sigmask(libc::SIG_SETMASK, &previous_mask, std::ptr::null_mut());
                if mask_result != 0 {
                    return Err(std::io::Error::from_raw_os_error(mask_result));
                }
                Ok(())
            });
        }
    }

    fn relay_pending(&self, process_group: libc::pid_t) -> Result<(), String> {
        loop {
            let mut pending: libc::sigset_t = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigpending(&mut pending) } != 0 {
                return Err(format!(
                    "failed to inspect pending launcher signals: {}",
                    std::io::Error::last_os_error()
                ));
            }
            if !FORWARDED_SIGNALS.iter().any(|signal| {
                (unsafe { libc::sigismember(&self.wait_set, *signal) } == 1)
                    && (unsafe { libc::sigismember(&pending, *signal) } == 1)
            }) {
                return Ok(());
            }

            let mut signal = 0;
            let wait_result = unsafe { libc::sigwait(&self.wait_set, &mut signal) };
            if wait_result != 0 {
                return Err(format!(
                    "failed to consume a pending launcher signal: {}",
                    std::io::Error::from_raw_os_error(wait_result)
                ));
            }
            let result = unsafe { libc::kill(-process_group, signal) };
            if result != 0 {
                let error = std::io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(format!(
                        "failed to relay signal {signal} to the sandbox process group: {error}"
                    ));
                }
            }
        }
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
    signal_relay: &SignalRelay,
    tracker: &mut DescendantTracker,
) -> Result<ExitStatus, String> {
    loop {
        if let Err(error) = tracker.drain_events() {
            let _ = kill_root(child);
            return Err(error);
        }

        match child.try_wait() {
            Ok(Some(status)) => return Ok(status),
            Ok(None) => {
                let process_group = child.id() as libc::pid_t;
                if let Err(error) = signal_relay.relay_pending(process_group) {
                    let _ = kill_root(child);
                    return Err(error);
                }
                thread::sleep(PROCESS_POLL_INTERVAL);
            }
            Err(error) => {
                let _ = kill_root(child);
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

#[derive(Clone, Copy, Eq, Hash, PartialEq)]
struct ProcessIdentity {
    pid: libc::pid_t,
    started_seconds: u64,
    started_microseconds: u64,
}

struct DescendantTracker {
    kqueue: OwnedFd,
    state: TrackerState,
}

impl DescendantTracker {
    fn start(root_pid: libc::pid_t) -> Result<Self, String> {
        let kqueue_descriptor = unsafe { libc::kqueue() };
        if kqueue_descriptor < 0 {
            return Err(format!(
                "failed to create the sandbox process tracker: {}",
                std::io::Error::last_os_error()
            ));
        }
        let kqueue = unsafe { OwnedFd::from_raw_fd(kqueue_descriptor) };

        // Darwin provides neither child subreapers nor PID namespaces, and its
        // kqueue NOTE_TRACK facility is unsupported. A descendant that becomes
        // orphaned before this post-spawn watch, or before a NOTE_FORK event can
        // be paired with a libproc snapshot, is therefore an intentional boundary
        // of the initial launcher. Once observed, descendants that call setsid(),
        // such as processx children, remain tracked by their PID and start time.
        let root = process_identity(root_pid)?;
        let mut state = TrackerState {
            root,
            active: HashMap::new(),
        };
        add_process_tree(kqueue.as_raw_fd(), root_pid, None, &mut state)?;

        Ok(Self { kqueue, state })
    }

    fn drain_events(&mut self) -> Result<(), String> {
        let mut events: [libc::kevent; TRACKER_EVENT_CAPACITY] =
            unsafe { std::mem::MaybeUninit::zeroed().assume_init() };
        let timeout = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };

        let event_count = loop {
            let result = unsafe {
                libc::kevent(
                    self.kqueue.as_raw_fd(),
                    std::ptr::null(),
                    0,
                    events.as_mut_ptr(),
                    events.len() as libc::c_int,
                    &timeout,
                )
            };
            if result < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(format!("sandbox process tracker failed: {error}"));
            }
            break result;
        };

        for event in events.iter().take(event_count as usize) {
            if event.filter != libc::EVFILT_PROC {
                continue;
            }

            let pid = event.ident as libc::pid_t;
            let event_data = event.data;
            if event.flags & libc::EV_ERROR != 0 {
                if event_data == libc::ESRCH as libc::intptr_t {
                    self.state.active.remove(&pid);
                    continue;
                }
                return Err(format!(
                    "sandbox process tracker received error {} for process {pid}",
                    event_data
                ));
            }

            if event.fflags & libc::NOTE_FORK != 0 {
                add_children(self.kqueue.as_raw_fd(), pid, &mut self.state)?;
            }
            if event.fflags & libc::NOTE_EXIT != 0 {
                self.state.active.remove(&pid);
            }
        }
        Ok(())
    }

    fn terminate(mut self) -> Result<(), String> {
        let deadline = Instant::now() + PROCESS_EXIT_TIMEOUT;
        loop {
            self.drain_events()?;

            // Re-snapshot before each signal pass to narrow the teardown fork
            // window. A child that becomes orphaned before observation remains
            // outside the documented supervision boundary.
            discover_active_children(self.kqueue.as_raw_fd(), &mut self.state)?;
            if let Some(root) = self.state.root {
                if self.state.active.get(&root.pid) == Some(&root) {
                    self.state.active.remove(&root.pid);
                }
            }
            remove_stale_processes(&mut self.state.active)?;

            // The root command has exited, so its background work has no
            // remaining lifetime to preserve.
            for identity in self.state.active.values().copied() {
                signal_process(identity, libc::SIGKILL)?;
            }
            remove_stale_processes(&mut self.state.active)?;

            if self.state.active.is_empty() {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err("timed out waiting for sandbox descendants to exit".to_string());
            }
            thread::sleep(PROCESS_POLL_INTERVAL);
        }
    }
}

struct TrackerState {
    root: Option<ProcessIdentity>,
    active: HashMap<libc::pid_t, ProcessIdentity>,
}

fn remove_stale_processes(
    active: &mut HashMap<libc::pid_t, ProcessIdentity>,
) -> Result<(), String> {
    let identities: Vec<_> = active.values().copied().collect();
    for identity in identities {
        if process_identity(identity.pid)? != Some(identity) {
            active.remove(&identity.pid);
        }
    }
    Ok(())
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
            if process_identity(pid)? != Some(identity) {
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
        if process_identity(parent.pid)? == Some(parent) {
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
        // libproc reports syscall failures as zero, so errno must be cleared
        // before the call to distinguish an empty result from an error.
        unsafe { *libc::__error() = 0 };
        let count = unsafe {
            libc::proc_listchildpids(
                parent,
                children.as_mut_ptr().cast(),
                std::mem::size_of_val(children.as_slice()) as libc::c_int,
            )
        };
        if count == 0 {
            let error_code = unsafe { *libc::__error() };
            if error_code == 0 {
                return Ok(Vec::new());
            }
            let error = std::io::Error::from_raw_os_error(error_code);
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!(
                "failed to list children of sandbox process {parent}: {error}"
            ));
        }
        if count < 0 {
            return Err(format!(
                "failed to list children of sandbox process {parent}: \
                 proc_listchildpids returned {count}"
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

fn process_info(pid: libc::pid_t) -> Result<Option<ProcessInfo>, String> {
    if pid <= 0 {
        return Err(format!("invalid sandbox process PID {pid}"));
    }

    let expected_size = std::mem::size_of::<libc::proc_bsdinfo>();
    loop {
        let mut info = std::mem::MaybeUninit::<libc::proc_bsdinfo>::zeroed();
        // Like proc_listchildpids(), proc_pidinfo() maps syscall failures to
        // zero and leaves the reason in errno.
        unsafe { *libc::__error() = 0 };
        let size = unsafe {
            libc::proc_pidinfo(
                pid,
                libc::PROC_PIDTBSDINFO,
                0,
                info.as_mut_ptr().cast(),
                expected_size as libc::c_int,
            )
        };
        if size as usize == expected_size {
            let info = unsafe { info.assume_init() };
            return Ok(Some(ProcessInfo {
                identity: ProcessIdentity {
                    pid,
                    started_seconds: info.pbi_start_tvsec,
                    started_microseconds: info.pbi_start_tvusec,
                },
                parent_pid: info.pbi_ppid as libc::pid_t,
            }));
        }

        let error_code = unsafe { *libc::__error() };
        if size == 0 && error_code == libc::ESRCH {
            return Ok(None);
        }
        if size == 0 && error_code == libc::EINTR {
            continue;
        }
        if size == 0 && error_code != 0 {
            return Err(format!(
                "failed to inspect sandbox process {pid}: {}",
                std::io::Error::from_raw_os_error(error_code)
            ));
        }
        return Err(format!(
            "failed to inspect sandbox process {pid}: \
             proc_pidinfo returned {size} bytes, expected {expected_size}"
        ));
    }
}

fn process_identity(pid: libc::pid_t) -> Result<Option<ProcessIdentity>, String> {
    Ok(process_info(pid)?.map(|info| info.identity))
}

fn signal_process(identity: ProcessIdentity, signal: libc::c_int) -> Result<bool, String> {
    if process_identity(identity.pid)? != Some(identity) {
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
