//! One runtime callback owns reconstruction and chunk reads independently of
//! worker event dispatch. Retirement cancels it without joining filesystem I/O.
use crate::relay_protocol::RelayCommand;
use crate::transcript::Transcript;
use crate::worker_client::platform::RelayCommandSender;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};

pub(super) struct ExportTask {
    requests: mpsc::SyncSender<()>,
    cancelled: Arc<AtomicBool>,
}

impl ExportTask {
    pub(super) fn start(
        recording: Result<Transcript, String>,
        commands: RelayCommandSender,
    ) -> Self {
        let (requests, chunks) = mpsc::sync_channel(1);
        let cancelled = Arc::new(AtomicBool::new(false));
        let cancellation = cancelled.clone();
        std::thread::spawn(move || {
            let snapshot = recording.and_then(|recording| recording.export_session(&cancellation));
            if cancellation.load(Ordering::Acquire) {
                return;
            }
            let mut contents = match snapshot {
                Ok((description, contents)) => {
                    if commands
                        .send(RelayCommand::SessionExported {
                            result: Ok(description),
                        })
                        .is_err()
                    {
                        return;
                    }
                    contents
                }
                Err(error) => {
                    let _ = commands.send(RelayCommand::SessionExported { result: Err(error) });
                    return;
                }
            };
            while chunks.recv().is_ok() {
                if cancellation.load(Ordering::Acquire) {
                    break;
                }
                let result = contents.next_chunk();
                if cancellation.load(Ordering::Acquire) {
                    break;
                }
                let failed = result.is_err();
                if commands
                    .send(RelayCommand::SessionChunkExported { result })
                    .is_err()
                    || failed
                {
                    break;
                }
            }
        });
        Self {
            requests,
            cancelled,
        }
    }

    pub(super) fn request_chunk(&self) -> Result<(), String> {
        self.requests.try_send(()).map_err(|error| match error {
            mpsc::TrySendError::Full(()) => "overlapping session export chunk requests".into(),
            mpsc::TrySendError::Disconnected(()) => "session export transfer stopped".into(),
        })
    }
}

impl Drop for ExportTask {
    fn drop(&mut self) {
        // Dropping requests also wakes a task waiting for the next chunk. A
        // blocked OS read can finish later, but must not send another reply.
        self.cancelled.store(true, Ordering::Release);
    }
}
