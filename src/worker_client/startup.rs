use std::mem::MaybeUninit;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::sync::Mutex;

use crate::resolver::ResolverStopHandle;

// The supported targets are macOS and Linux. `cfg(unix)` on shared runtime
// modules describes their API requirements, not support for other Unix targets.
#[cfg(target_os = "macos")]
#[path = "startup/macos.rs"]
mod platform;
#[cfg(target_os = "linux")]
#[path = "startup/linux.rs"]
mod platform;

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

    let startup = Mutex::new(Startup::default());
    std::thread::scope(|scope| {
        // This writer is dropped before the scoped thread is joined, including
        // when initialization unwinds. The watcher never consumes MCP input.
        let (completed, completion) = UnixStream::pair()
            .map_err(|error| format!("failed to create MCP startup completion pipe: {error}"))?;
        let queue = platform::InputWatch::new(completion.as_raw_fd())?;
        let watcher = scope.spawn(|| {
            if let Err(error) = queue.wait(completion) {
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
