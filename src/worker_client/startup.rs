use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::sync::Mutex;

use crate::resolver::ResolverStopHandle;

#[derive(Default)]
struct Startup {
    cancelled: Option<String>,
    resolver: Option<ResolverStopHandle>,
}

pub(super) fn with_input_owner<T>(
    initialize: impl FnOnce(&dyn Fn(ResolverStopHandle) -> Result<(), String>) -> Result<T, String>,
) -> Result<T, String> {
    let mut input = MaybeUninit::<libc::stat>::uninit();
    // SAFETY: `input` points to writable storage for the descriptor's status.
    if unsafe { libc::fstat(libc::STDIN_FILENO, input.as_mut_ptr()) } != 0 {
        return Err(format!(
            "failed to inspect MCP input: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: successful fstat initialized the status.
    let kind = unsafe { input.assume_init() }.st_mode & libc::S_IFMT;
    // Only pipes and sockets report peer closure without consuming input.
    if !matches!(kind, libc::S_IFIFO | libc::S_IFSOCK) {
        return initialize(&|_| Ok(()));
    }

    // SAFETY: kqueue returns a new owned descriptor on success.
    let descriptor = unsafe { libc::kqueue() };
    if descriptor < 0 {
        return Err(format!(
            "failed to watch MCP input: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: `descriptor` is the newly created kqueue, owned only here.
    let queue = unsafe { OwnedFd::from_raw_fd(descriptor) };
    // SAFETY: the queue is live; prevent resolver children from inheriting it.
    if unsafe { libc::fcntl(descriptor, libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
        return Err(format!(
            "failed to configure MCP input watch: {}",
            std::io::Error::last_os_error()
        ));
    }
    let startup = Mutex::new(Startup::default());
    std::thread::scope(|scope| {
        // This writer is dropped before the scoped thread is joined, including
        // when initialization unwinds. The watcher never consumes MCP input.
        let (completed, completion) = UnixStream::pair()
            .map_err(|error| format!("failed to create MCP startup completion pipe: {error}"))?;
        let changes = [libc::STDIN_FILENO, completion.as_raw_fd()].map(|descriptor| libc::kevent {
            ident: descriptor as libc::uintptr_t,
            filter: libc::EVFILT_READ,
            flags: libc::EV_ADD | libc::EV_CLEAR,
            fflags: 0,
            data: 0,
            udata: std::ptr::null_mut(),
        });
        submit(&queue, &changes)?;
        let watcher = scope.spawn(|| {
            if let Err(error) = watch(&queue, completion) {
                let mut startup = startup.lock().expect("startup state is not poisoned");
                startup.cancelled = Some(error);
                if let Some(resolver) = &startup.resolver
                    && let Err(error) = resolver.stop()
                {
                    startup.cancelled = Some(format!("failed to cancel startup resolver: {error}"));
                }
            }
        });
        let result = initialize(&|resolver| {
            let mut startup = startup.lock().expect("startup state is not poisoned");
            if let Some(error) = &startup.cancelled {
                return Err(error.clone());
            }
            startup.resolver = Some(resolver);
            Ok(())
        });
        drop(completed);
        watcher.join().expect("MCP startup watcher did not panic");
        match startup
            .lock()
            .expect("startup state is not poisoned")
            .cancelled
            .take()
        {
            Some(error) => Err(error),
            None => result,
        }
    })
}

fn submit(queue: &OwnedFd, changes: &[libc::kevent]) -> Result<(), String> {
    loop {
        // SAFETY: changes is valid for its length; this call returns no events.
        let result = unsafe {
            libc::kevent(
                queue.as_raw_fd(),
                changes.as_ptr(),
                changes.len() as _,
                std::ptr::null_mut(),
                0,
                std::ptr::null(),
            )
        };
        if result >= 0 {
            return Ok(());
        }
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(format!("failed to register MCP input watch: {error}"));
        }
    }
}

fn watch(queue: &OwnedFd, completion: UnixStream) -> Result<(), String> {
    loop {
        // SAFETY: zero is valid for every kevent field.
        let mut events: [libc::kevent; 2] = unsafe { std::mem::zeroed() };
        // SAFETY: events is writable for its length; no changes or timeout are supplied.
        let count = unsafe {
            libc::kevent(
                queue.as_raw_fd(),
                std::ptr::null(),
                0,
                events.as_mut_ptr(),
                events.len() as _,
                std::ptr::null(),
            )
        };
        if count < 0 {
            let error = std::io::Error::last_os_error();
            if error.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(format!("failed to wait for MCP input closure: {error}"));
        }
        for event in &events[..count as usize] {
            if event.ident == libc::STDIN_FILENO as libc::uintptr_t
                && event.flags & libc::EV_EOF != 0
            {
                return Err("server startup cancelled because MCP input closed".to_string());
            }
        }
        if events[..count as usize]
            .iter()
            .any(|event| event.ident == completion.as_raw_fd() as libc::uintptr_t)
        {
            return Ok(());
        }
    }
}
