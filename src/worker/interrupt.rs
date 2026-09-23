use std::io;
use std::sync::OnceLock;

// These callbacks only inspect or update interrupt state; they must not enter
// an interpreter. The signal callback must also be async-signal-safe. In the
// mixed runtime, R supplies the state so nested calls share one acknowledgment.
pub(super) struct State {
    pub signal: unsafe extern "C" fn(),
    pub pending: fn() -> bool,
    pub clear: fn(),
}

static STATE: OnceLock<State> = OnceLock::new();

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

pub(crate) fn pending() -> bool {
    (STATE.get().expect("interrupt state initialized").pending)()
}

pub(crate) fn acknowledge_python_interrupt() -> bool {
    if !pending() {
        return false;
    }
    (STATE.get().expect("interrupt state initialized").clear)();
    true
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
