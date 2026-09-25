use std::io;
#[cfg(target_os = "macos")]
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};

// These callbacks only inspect or update interrupt state; they must not enter
// an interpreter. The signal callback must also be async-signal-safe. In the
// mixed runtime, R supplies the state so nested calls share one acknowledgment.
pub(super) struct State {
    pub signal: unsafe extern "C" fn(),
    pub pending: fn() -> bool,
    pub acknowledge: fn() -> bool,
}

static STATE: OnceLock<State> = OnceLock::new();
static NATIVE_PENDING: AtomicBool = AtomicBool::new(false);
#[cfg(target_os = "macos")]
static NATIVE_INPUT_WATCH: OnceLock<OwnedFd> = OnceLock::new();

unsafe extern "C" fn record_native_interrupt() {
    NATIVE_PENDING.store(true, Ordering::SeqCst);
}

fn native_pending() -> bool {
    NATIVE_PENDING.load(Ordering::SeqCst)
}

fn acknowledge_native_interrupt() -> bool {
    NATIVE_PENDING.swap(false, Ordering::SeqCst)
}

unsafe extern "C" {
    fn mcp_worker_interrupt_configure(
        signal: unsafe extern "C" fn(),
        wakeup: libc::c_int,
    ) -> libc::c_int;
    fn mcp_worker_install_python_interrupt(set_interrupt: unsafe extern "C" fn()) -> libc::c_int;
}

pub(super) fn normalize_signal() -> io::Result<()> {
    if unsafe { libc::signal(libc::SIGINT, libc::SIG_DFL) } == libc::SIG_ERR {
        return Err(io::Error::last_os_error());
    }
    let mut signals = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigemptyset(&mut signals) } != 0
        || unsafe { libc::sigaddset(&mut signals, libc::SIGINT) } != 0
    {
        return Err(io::Error::last_os_error());
    }
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) };
    (result == 0)
        .then_some(())
        .ok_or_else(|| io::Error::from_raw_os_error(result))
}

pub(super) fn initialize(state: State) -> io::Result<()> {
    let signal = state.signal;
    STATE
        .set(state)
        .map_err(|_| io::Error::other("interrupt state already initialized"))?;
    let wakeup = super::input::initialize_interrupt_wakeup()?;
    if unsafe { mcp_worker_interrupt_configure(signal, wakeup) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(super) fn initialize_native() -> io::Result<()> {
    #[cfg(target_os = "macos")]
    initialize_native_input_watch()?;
    initialize(State {
        signal: record_native_interrupt,
        pending: native_pending,
        acknowledge: acknowledge_native_interrupt,
    })
}

#[cfg(target_os = "macos")]
fn initialize_native_input_watch() -> io::Result<()> {
    let descriptor = unsafe { libc::kqueue() };
    if descriptor < 0 {
        return Err(io::Error::last_os_error());
    }
    let queue = unsafe { OwnedFd::from_raw_fd(descriptor) };
    if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let change = libc::kevent {
        ident: libc::STDIN_FILENO as libc::uintptr_t,
        filter: libc::EVFILT_READ,
        flags: libc::EV_ADD | libc::EV_CLEAR,
        fflags: 0,
        data: 0,
        udata: std::ptr::null_mut(),
    };
    if unsafe {
        libc::kevent(
            descriptor,
            &change,
            1,
            std::ptr::null_mut(),
            0,
            std::ptr::null(),
        )
    } < 0
    {
        return Err(io::Error::last_os_error());
    }
    NATIVE_INPUT_WATCH
        .set(queue)
        .map_err(|_| io::Error::other("native input watch already initialized"))
}

pub(super) fn wait_for_activity(sideband_fd: libc::c_int) -> Result<bool, String> {
    let wakeup_fd = super::input::interrupt_wakeup_fd();
    let mut descriptors = [
        libc::pollfd {
            fd: sideband_fd,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: wakeup_fd,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: native_input_watch_fd(),
            events: native_input_watch_events(),
            revents: 0,
        },
    ];
    loop {
        let ready = unsafe { libc::poll(descriptors.as_mut_ptr(), descriptors.len() as _, -1) };
        if ready < 0 {
            let error = io::Error::last_os_error();
            if error.kind() == io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("native worker wait failed: {error}"));
        }
        if descriptors
            .iter()
            .any(|entry| entry.revents & libc::POLLNVAL != 0)
        {
            return Err("native worker wait received an invalid descriptor".to_string());
        }
        if descriptors[1].revents != 0 {
            super::input::drain_interrupt_wakeup().map_err(|error| error.to_string())?;
        }
        if descriptors[0].revents != 0 {
            return Ok(true);
        }
        if descriptors[2].revents & libc::POLLERR != 0 {
            return Err("native worker stdin wait failed".to_string());
        }
        #[cfg(target_os = "macos")]
        if descriptors[2].revents != 0 {
            observe_native_input_watch()?;
        }
        return Ok(false);
    }
}

#[cfg(target_os = "linux")]
fn native_input_watch_fd() -> libc::c_int {
    libc::STDIN_FILENO
}

#[cfg(target_os = "linux")]
fn native_input_watch_events() -> libc::c_short {
    libc::POLLRDHUP
}

#[cfg(target_os = "macos")]
fn native_input_watch_fd() -> libc::c_int {
    NATIVE_INPUT_WATCH
        .get()
        .expect("native input watch initialized")
        .as_raw_fd()
}

#[cfg(target_os = "macos")]
fn native_input_watch_events() -> libc::c_short {
    libc::POLLIN
}

#[cfg(target_os = "macos")]
fn observe_native_input_watch() -> Result<(), String> {
    let mut event = unsafe { std::mem::zeroed() };
    let count = unsafe {
        libc::kevent(
            native_input_watch_fd(),
            std::ptr::null(),
            0,
            &mut event,
            1,
            std::ptr::null(),
        )
    };
    if count < 0 {
        return Err(format!(
            "native worker stdin watch failed: {}",
            io::Error::last_os_error()
        ));
    }
    if event.flags & libc::EV_EOF != 0 {
        super::core::mark_shutting_down();
    }
    Ok(())
}

pub(super) fn pending() -> bool {
    (STATE.get().expect("interrupt state initialized").pending)()
}

pub(crate) fn acknowledge_python_interrupt() -> bool {
    (STATE
        .get()
        .expect("interrupt state initialized")
        .acknowledge)()
}

pub(crate) fn install_python_interrupt(
    set_interrupt: unsafe extern "C" fn(),
) -> Result<(), String> {
    if unsafe { mcp_worker_install_python_interrupt(set_interrupt) } != 0 {
        return Err(format!(
            "failed to install Python interrupt handler: {}",
            io::Error::last_os_error()
        ));
    }
    Ok(())
}

/// Connect inspection cancellation to the worker's existing SIGINT wakeup.
/// ResolverProcess continues to own termination, output collection and reaping.
pub(crate) fn inspect_python(
    executable: &std::path::Path,
) -> Result<crate::python::SelectedPython, String> {
    super::input::drain_interrupt_wakeup().map_err(|error| error.to_string())?;
    let (finished, completion) = io::pipe().map_err(|error| error.to_string())?;
    std::thread::scope(|scope| {
        let mut watcher = None;
        let result = crate::python::inspect_selected(executable, |handle| {
            if pending() {
                return Err("Python inspection interrupted".to_string());
            }
            watcher = Some(scope.spawn(move || {
                match crate::readiness::wait_for_io(
                    super::input::interrupt_wakeup_fd(),
                    libc::POLLIN,
                    Some(&finished),
                ) {
                    Ok(ready) if ready.stream => handle.stop(),
                    Ok(_) => Ok(()),
                    Err(error) => {
                        let _ = handle.stop();
                        Err(error.to_string())
                    }
                }
            }));
            Ok(())
        });
        drop(completion);
        if let Some(watcher) = watcher {
            watcher
                .join()
                .map_err(|_| "Python inspection interrupt watcher panicked")??;
        }
        result
    })
}
