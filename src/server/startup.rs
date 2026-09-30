//! Share one preparation outcome while the MCP connection owns cancellation.
#[cfg(unix)]
use std::mem::MaybeUninit;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};

use tokio::sync::watch;

pub(super) struct Runtime {
    pub worker: crate::worker_client::Client,
    pub transcript: crate::transcript::Transcript,
}

#[derive(Clone)]
pub(super) struct Startup {
    result: watch::Receiver<Option<Result<Arc<Runtime>, String>>>,
    cancellation: Arc<Mutex<Cancellation>>,
}

#[derive(Default)]
struct Cancellation {
    closed: bool,
    resolver: Option<crate::resolver::ResolverStopHandle>,
}

impl Startup {
    pub fn new(
        input_closed: super::InputClosed,
        initialize: impl FnOnce(
            &dyn Fn(crate::resolver::ResolverStopHandle) -> Result<(), String>,
        ) -> Result<Runtime, String>
        + Send
        + 'static,
    ) -> Self {
        let (ready, result) = watch::channel(None);
        let cancellation = Arc::new(Mutex::new(Cancellation::default()));
        let control = Arc::clone(&cancellation);
        tokio::spawn(async move {
            let result = tokio::task::spawn_blocking(move || {
                observe_input(input_closed, || {
                    initialize(&|resolver| {
                        let mut control = control.lock().expect("startup cancellation lock");
                        if control.closed {
                            return Err("MCP connection closed during runtime preparation".into());
                        }
                        control.resolver = Some(resolver);
                        Ok(())
                    })
                })
            })
            .await
            .map_err(|error| format!("runtime preparation task failed: {error}"))
            .and_then(|result| result)
            .map(Arc::new);
            ready.send_replace(Some(result));
        });
        Self {
            result,
            cancellation,
        }
    }

    pub async fn ready(&self) -> Result<Arc<Runtime>, String> {
        let mut result = self.result.clone();
        let ready = result
            .wait_for(Option::is_some)
            .await
            .map_err(|_| "runtime preparation stopped without a result".to_string())?;
        ready
            .as_ref()
            .expect("runtime preparation completed")
            .clone()
    }

    pub async fn cancel(&self) -> Result<(), String> {
        if self.result.borrow().is_some() {
            return Ok(());
        }
        let control = Arc::clone(&self.cancellation);
        tokio::task::spawn_blocking(move || {
            let resolver = {
                let mut control = control.lock().expect("startup cancellation lock");
                control.closed = true;
                control.resolver.clone()
            };
            resolver.map_or(Ok(()), |resolver| resolver.stop())
        })
        .await
        .map_err(|error| format!("startup cancellation task failed: {error}"))?
    }

    pub fn finish_failed_preparation(&self, error: String) -> Result<(), String> {
        // A failed initializer has no Client to own shutdown. Suppress only
        // connection cancellation with confirmed cleanup; retain other failures.
        let control = self.cancellation.lock().expect("startup cancellation lock");
        if control.closed
            && control
                .resolver
                .as_ref()
                .is_none_or(|resolver| resolver.cleanup_confirmed())
        {
            Ok(())
        } else {
            Err(error)
        }
    }
}

/// Observe peer closure without consuming queued MCP messages, even when the
/// protocol task is blocked writing its initialization response.
#[cfg(unix)]
fn observe_input<T>(
    input_closed: super::InputClosed,
    initialize: impl FnOnce() -> Result<T, String>,
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
    if !matches!(kind, libc::S_IFIFO | libc::S_IFSOCK) {
        return initialize();
    }
    std::thread::scope(|scope| {
        let (completed, completion) = UnixStream::pair()
            .map_err(|error| format!("failed to create MCP startup completion pipe: {error}"))?;
        let queue = crate::input_watch::InputWatch::new(completion.as_raw_fd())?;
        let watcher = scope.spawn(|| {
            let result = queue.wait(completion);
            if result.is_err() {
                input_closed.close();
            }
        });
        let result = initialize();
        drop(completed);
        watcher.join().expect("MCP startup watcher did not panic");
        result
    })
}

#[cfg(not(unix))]
fn observe_input<T>(
    _input_closed: super::InputClosed,
    initialize: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    initialize()
}
