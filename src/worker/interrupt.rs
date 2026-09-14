use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, AtomicPtr, Ordering};

static PENDING: AtomicBool = AtomicBool::new(false);
static R_PENDING: AtomicPtr<libc::c_int> = AtomicPtr::new(std::ptr::null_mut());
static PYTHON_INTERRUPT: AtomicPtr<()> = AtomicPtr::new(std::ptr::null_mut());
static WAKEUP: OnceLock<[OwnedFd; 2]> = OnceLock::new();

pub(super) fn initialize() -> Result<(), String> {
    let mut descriptors = [-1; 2];
    if unsafe { libc::pipe(descriptors.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    let descriptors = descriptors.map(|fd| unsafe { OwnedFd::from_raw_fd(fd) });
    for descriptor in &descriptors {
        let fd = descriptor.as_raw_fd();
        if unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) } < 0
            || unsafe { libc::fcntl(fd, libc::F_SETFL, libc::O_NONBLOCK) } < 0
        {
            return Err(io::Error::last_os_error().to_string());
        }
    }
    WAKEUP
        .set(descriptors)
        .map_err(|_| "interrupt wakeup already initialized")?;
    install()
}

pub(crate) fn install() -> Result<(), String> {
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = interrupt as *const () as libc::sighandler_t;
    unsafe { libc::sigemptyset(&mut action.sa_mask) };
    if unsafe { libc::sigaction(libc::SIGINT, &action, std::ptr::null_mut()) } != 0 {
        return Err(io::Error::last_os_error().to_string());
    }
    let mut signals = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut signals);
        libc::sigaddset(&mut signals, libc::SIGINT);
    }
    let result =
        unsafe { libc::pthread_sigmask(libc::SIG_UNBLOCK, &signals, std::ptr::null_mut()) };
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result).to_string());
    }
    Ok(())
}

pub(super) fn set_r_pending(pointer: *mut libc::c_int) {
    R_PENDING.store(pointer, Ordering::SeqCst);
}

pub(crate) fn set_python_interrupt(function: unsafe extern "C" fn()) {
    PYTHON_INTERRUPT.store(function as *mut (), Ordering::SeqCst);
}

extern "C" fn interrupt(_: libc::c_int) {
    PENDING.store(true, Ordering::SeqCst);
    let r = R_PENDING.load(Ordering::SeqCst);
    if !r.is_null() {
        unsafe { std::ptr::write_volatile(r, 1) };
    }
    let python = PYTHON_INTERRUPT.load(Ordering::SeqCst);
    if !python.is_null() {
        // PyErr_SetInterrupt is explicitly async-signal-safe. Console's
        // Python SIGINT handler is installed before publication.
        let signal: unsafe extern "C" fn() = unsafe { std::mem::transmute(python) };
        unsafe { signal() };
    }
    wake();
}

pub(super) fn wake() {
    if let Some(wakeup) = WAKEUP.get() {
        unsafe { libc::write(wakeup[1].as_raw_fd(), b"i".as_ptr().cast(), 1) };
    }
}

pub(super) fn pending() -> bool {
    if R_PENDING.load(Ordering::SeqCst).is_null() {
        PENDING.load(Ordering::SeqCst)
    } else {
        super::embedded_r::console_interrupt_pending()
    }
}

/// Native R and Python may be nested on the same thread. Both receive the
/// signal; the first runtime to reach its safe boundary consumes it. R clears
/// its pending flag when raising an R interrupt, so Python must acknowledge
/// that consumption instead of raising again after the nested call returns.
pub(crate) fn take_python() -> bool {
    let r = R_PENDING.load(Ordering::SeqCst);
    if !r.is_null() && unsafe { std::ptr::read_volatile(r) } != 0 && !pending() {
        let python = PYTHON_INTERRUPT.load(Ordering::SeqCst);
        let signal: unsafe extern "C" fn() = unsafe { std::mem::transmute(python) };
        unsafe { signal() };
        return false;
    }
    let pending = pending();
    if pending {
        clear();
    }
    pending
}

pub(super) fn clear() {
    PENDING.store(false, Ordering::SeqCst);
    super::embedded_r::discard_interrupts();
    drain();
}

fn drain() {
    let fd = WAKEUP.get().expect("interrupt wakeup")[0].as_raw_fd();
    let mut bytes = [0u8; 256];
    while unsafe { libc::read(fd, bytes.as_mut_ptr().cast(), bytes.len()) } > 0 {}
}

pub(super) fn wait(descriptors: &mut [libc::pollfd]) -> Result<libc::c_int, String> {
    let mut events = descriptors.to_vec();
    events.push(libc::pollfd {
        fd: WAKEUP.get().expect("interrupt wakeup")[0].as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    });
    let result = unsafe { libc::poll(events.as_mut_ptr(), events.len() as _, -1) };
    if events[descriptors.len()].revents != 0 {
        drain();
    }
    descriptors.copy_from_slice(&events[..descriptors.len()]);
    Ok(result)
}
