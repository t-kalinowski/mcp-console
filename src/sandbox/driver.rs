use std::future::Future;
use std::io::{self, Read, Write};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;

use codex_sandbox_api::{SandboxedOutput as ApiOutput, SandboxedStdin as ApiStdin};

#[derive(Clone)]
pub(super) struct Driver(Arc<DriverInner>);

struct DriverInner {
    handle: tokio::runtime::Handle,
    shutdown: Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
    thread: Mutex<Option<thread::JoinHandle<()>>>,
    thread_id: thread::ThreadId,
}

impl Driver {
    pub(super) fn new() -> Result<Self, String> {
        let (shutdown, shutdown_receiver) = tokio::sync::oneshot::channel();
        let (started, startup) = mpsc::sync_channel(1);
        let thread = thread::Builder::new()
            .name("mcp-console-sandbox-driver".to_string())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_current_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(error) => {
                        let _ = started.send(Err(format!(
                            "failed to create sandbox driver runtime: {error}"
                        )));
                        return;
                    }
                };
                let _ = started.send(Ok((runtime.handle().clone(), thread::current().id())));
                runtime.block_on(async {
                    let _ = shutdown_receiver.await;
                });
            })
            .map_err(|error| format!("failed to start sandbox driver: {error}"))?;
        let (handle, thread_id) = startup
            .recv()
            .map_err(|_| "sandbox driver stopped during startup".to_string())??;
        Ok(Self(Arc::new(DriverInner {
            handle,
            shutdown: Mutex::new(Some(shutdown)),
            thread: Mutex::new(Some(thread)),
            thread_id,
        })))
    }

    pub(super) fn run<T>(
        &self,
        future: impl Future<Output = Result<T, String>> + Send + 'static,
    ) -> Result<T, String>
    where
        T: Send + 'static,
    {
        let (completed, result) = mpsc::sync_channel(1);
        self.0.handle.spawn(async move {
            let _ = completed.send(future.await);
        });
        result
            .recv()
            .map_err(|_| "sandbox driver task stopped before completion".to_string())?
    }

    fn spawn(&self, future: impl Future<Output = ()> + Send + 'static) {
        self.0.handle.spawn(future);
    }
}

impl Drop for DriverInner {
    fn drop(&mut self) {
        if let Some(shutdown) = self
            .shutdown
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
        {
            let _ = shutdown.send(());
        }
        let thread = self
            .thread
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take();
        if thread::current().id() != self.thread_id
            && let Some(thread) = thread
        {
            let _ = thread.join();
        }
    }
}

enum StdinRequest {
    Write {
        bytes: Vec<u8>,
        completed: mpsc::SyncSender<Result<(), String>>,
    },
}

#[derive(Clone)]
pub(crate) struct SandboxIoCancellation(Arc<Mutex<Option<tokio::sync::oneshot::Sender<()>>>>);

impl SandboxIoCancellation {
    fn new() -> (Self, tokio::sync::oneshot::Receiver<()>) {
        let (sender, receiver) = tokio::sync::oneshot::channel();
        (Self(Arc::new(Mutex::new(Some(sender)))), receiver)
    }

    pub(crate) fn cancel(&self) {
        let sender = self
            .0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take();
        if let Some(sender) = sender {
            let _ = sender.send(());
        }
    }
}

pub(crate) struct SandboxedStdin {
    requests: Option<tokio::sync::mpsc::Sender<StdinRequest>>,
    cancellation: SandboxIoCancellation,
    finished: Option<mpsc::Receiver<()>>,
}

impl SandboxedStdin {
    pub(super) fn new(
        driver: &Driver,
        mut stdin: ApiStdin,
        keepalive: impl Send + 'static,
    ) -> Self {
        let (requests, mut receiver) = tokio::sync::mpsc::channel(1);
        let (cancellation, mut cancelled) = SandboxIoCancellation::new();
        let (finished, completion) = mpsc::channel();
        driver.spawn(async move {
            let mut was_cancelled = false;
            loop {
                let request = tokio::select! {
                    _ = &mut cancelled => {
                        was_cancelled = true;
                        break;
                    }
                    request = receiver.recv() => request,
                };
                let Some(StdinRequest::Write { bytes, completed }) = request else {
                    break;
                };
                let result = tokio::select! {
                    _ = &mut cancelled => {
                        was_cancelled = true;
                        Err("sandbox stdin cancelled".to_string())
                    }
                    result = stdin.write_all(&bytes) => result.map_err(|error| error.to_string()),
                };
                let failed = result.is_err();
                let _ = completed.send(result);
                if failed {
                    break;
                }
            }
            if was_cancelled {
                drop(stdin);
            } else {
                let _ = stdin.close().await;
            }
            drop(keepalive);
            let _ = finished.send(());
        });
        Self {
            requests: Some(requests),
            cancellation,
            finished: Some(completion),
        }
    }

    pub(crate) fn cancellation(&self) -> SandboxIoCancellation {
        self.cancellation.clone()
    }
}

impl Write for SandboxedStdin {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.is_empty() {
            return Ok(0);
        }
        let (completed, result) = mpsc::sync_channel(1);
        self.requests
            .as_ref()
            .ok_or_else(|| io::Error::new(io::ErrorKind::BrokenPipe, "sandbox stdin closed"))?
            .blocking_send(StdinRequest::Write {
                bytes: bytes.to_vec(),
                completed,
            })
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "sandbox stdin closed"))?;
        result
            .recv()
            .map_err(|_| io::Error::new(io::ErrorKind::BrokenPipe, "sandbox stdin task stopped"))?
            .map_err(io::Error::other)?;
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Drop for SandboxedStdin {
    fn drop(&mut self) {
        self.requests.take();
        self.cancellation.cancel();
        if let Some(finished) = self.finished.take() {
            let _ = finished.recv();
        }
    }
}

pub(crate) struct SandboxedOutput {
    chunks: tokio::sync::mpsc::Receiver<Result<Vec<u8>, String>>,
    current: Vec<u8>,
    offset: usize,
    cancellation: SandboxIoCancellation,
    finished: Option<mpsc::Receiver<()>>,
}

impl SandboxedOutput {
    pub(super) fn new(
        driver: &Driver,
        mut output: ApiOutput,
        keepalive: impl Send + 'static,
    ) -> Self {
        let (chunks, receiver) = tokio::sync::mpsc::channel(1);
        let (cancellation, mut cancelled) = SandboxIoCancellation::new();
        let (finished, completion) = mpsc::channel();
        driver.spawn(async move {
            loop {
                let result = tokio::select! {
                    _ = &mut cancelled => break,
                    result = output.read_chunk() => result,
                };
                let chunk = match result {
                    Ok(Some(chunk)) => Ok(chunk),
                    Ok(None) => break,
                    Err(error) => Err(error.to_string()),
                };
                let failed = chunk.is_err();
                if chunks.send(chunk).await.is_err() || failed {
                    break;
                }
            }
            drop(output);
            drop(keepalive);
            let _ = finished.send(());
        });
        Self {
            chunks: receiver,
            current: Vec::new(),
            offset: 0,
            cancellation,
            finished: Some(completion),
        }
    }

    pub(crate) fn cancellation(&self) -> SandboxIoCancellation {
        self.cancellation.clone()
    }
}

impl Read for SandboxedOutput {
    fn read(&mut self, destination: &mut [u8]) -> io::Result<usize> {
        if destination.is_empty() {
            return Ok(0);
        }
        while self.offset == self.current.len() {
            self.current.clear();
            self.offset = 0;
            let Some(chunk) = self.chunks.blocking_recv() else {
                return Ok(0);
            };
            self.current = chunk.map_err(io::Error::other)?;
        }

        let count = destination.len().min(self.current.len() - self.offset);
        destination[..count].copy_from_slice(&self.current[self.offset..self.offset + count]);
        self.offset += count;
        Ok(count)
    }
}

impl Drop for SandboxedOutput {
    fn drop(&mut self) {
        self.chunks.close();
        self.cancellation.cancel();
        if let Some(finished) = self.finished.take() {
            let _ = finished.recv();
        }
    }
}
