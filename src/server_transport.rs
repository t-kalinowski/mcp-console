use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use rmcp::RoleServer;
use rmcp::model::{ClientNotification, GetExtensions, JsonRpcMessage, RequestId};
use rmcp::service::{RxJsonRpcMessage, TxJsonRpcMessage};
use rmcp::transport::{Transport, async_rw::AsyncRwTransport};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::Notify;

use crate::worker_client::ResponseDelivery;

#[derive(Clone, Default)]
pub(crate) struct ResponseDeliveries {
    state: Arc<Mutex<ResponseDeliveryState>>,
    write_finished: Arc<Notify>,
}

#[derive(Clone)]
pub(crate) struct ResponseDeliveryCall(Arc<Mutex<ResponseDeliveryCallState>>);

#[derive(Default)]
struct ResponseDeliveryState {
    active: HashMap<RequestId, ResponseDeliveryCall>,
    /// Evaluation-response futures registered before they run on the service task.
    pending_writes: usize,
    closed: bool,
}

struct ResponseDeliveryCallState {
    active: bool,
    delivery: Option<ResponseDelivery>,
}

/// Keeps one request's delivery claim registered until its response write finishes.
struct ResponseDeliveryWrite {
    deliveries: ResponseDeliveries,
    request_id: RequestId,
    call: ResponseDeliveryCall,
    tracks_delivery: bool,
    finished: bool,
}

impl ResponseDeliveries {
    fn start(&self, request_id: RequestId) -> ResponseDeliveryCall {
        let mut state = self.lock();
        let call = ResponseDeliveryCall::new(!state.closed);
        let replaced = if state.closed {
            None
        } else {
            state.active.insert(request_id, call.clone())
        };
        let replaced = replaced.and_then(|replaced| replaced.finish());
        drop(state);
        if let Some(replaced) = replaced {
            replaced.unclaimed();
        }
        call
    }

    pub(crate) fn cancel(&self, request_id: &RequestId) {
        let delivery = {
            let mut state = self.lock();
            state
                .active
                .remove(request_id)
                .and_then(|call| call.finish())
        };
        if let Some(delivery) = delivery {
            delivery.unclaimed();
        }
    }

    fn write(&self, request_id: &RequestId) -> Option<ResponseDeliveryWrite> {
        let (call, tracks_delivery) = {
            let mut state = self.lock();
            let call = state.active.get(request_id).cloned()?;
            let tracks_delivery = call.has_delivery();
            if tracks_delivery {
                state.pending_writes += 1;
            }
            (call, tracks_delivery)
        };
        Some(ResponseDeliveryWrite {
            deliveries: self.clone(),
            request_id: request_id.clone(),
            call,
            tracks_delivery,
            finished: false,
        })
    }

    fn close(&self) {
        let deliveries = {
            let mut state = self.lock();
            state.closed = true;
            state
                .active
                .drain()
                .filter_map(|(_, call)| call.finish())
                .collect::<Vec<_>>()
        };
        for delivery in deliveries {
            delivery.unclaimed();
        }
    }

    fn take_write_delivery(
        &self,
        request_id: &RequestId,
        expected: &ResponseDeliveryCall,
    ) -> Option<ResponseDelivery> {
        let mut state = self.lock();
        let current = state.active.get(request_id)?;
        if !current.is(expected) {
            return None;
        }
        state
            .active
            .remove(request_id)
            .and_then(|call| call.finish())
    }

    fn settle_write(&self) {
        {
            let mut state = self.lock();
            assert!(
                state.pending_writes > 0,
                "a response write must be pending before it settles"
            );
            state.pending_writes -= 1;
        }
        self.write_finished.notify_one();
    }

    async fn wait_for_writes(&self) {
        loop {
            let write_finished = self.write_finished.notified();
            if self.lock().pending_writes == 0 {
                return;
            }
            write_finished.await;
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl ResponseDeliveryCall {
    fn new(active: bool) -> Self {
        Self(Arc::new(Mutex::new(ResponseDeliveryCallState {
            active,
            delivery: None,
        })))
    }

    pub(crate) fn register(&self, delivery: ResponseDelivery) {
        let mut state = self.lock();
        if !state.active {
            drop(state);
            delivery.unclaimed();
            return;
        }
        assert!(
            state.delivery.replace(delivery).is_none(),
            "a response can register only one delivery acknowledgment"
        );
    }

    fn finish(&self) -> Option<ResponseDelivery> {
        let mut state = self.lock();
        state.active = false;
        state.delivery.take()
    }

    fn is(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }

    fn has_delivery(&self) -> bool {
        self.lock().delivery.is_some()
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryCallState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl ResponseDeliveryWrite {
    fn delivered(mut self) {
        self.finish(ResponseDelivery::delivered);
    }

    fn unclaimed(mut self) {
        self.finish(ResponseDelivery::unclaimed);
    }

    fn finish(&mut self, settle_delivery: impl FnOnce(ResponseDelivery)) {
        if self.finished {
            return;
        }
        self.finished = true;
        if let Some(delivery) = self
            .deliveries
            .take_write_delivery(&self.request_id, &self.call)
        {
            settle_delivery(delivery);
        }
        if self.tracks_delivery {
            self.deliveries.settle_write();
        }
    }
}

impl Drop for ResponseDeliveryWrite {
    fn drop(&mut self) {
        self.finish(ResponseDelivery::unclaimed);
    }
}

pub(crate) struct ServerTransport<R: AsyncRead, W: AsyncWrite> {
    inner: AsyncRwTransport<RoleServer, R, W>,
    deliveries: ResponseDeliveries,
}

impl<R, W> ServerTransport<R, W>
where
    R: AsyncRead + Send + Unpin,
    W: AsyncWrite + Send + Unpin + 'static,
{
    pub(crate) fn new(read: R, write: W, deliveries: ResponseDeliveries) -> Self {
        Self {
            inner: AsyncRwTransport::new_server(read, write),
            deliveries,
        }
    }
}

impl<R, W> Transport<RoleServer> for ServerTransport<R, W>
where
    R: AsyncRead + Send + Unpin,
    W: AsyncWrite + Send + Unpin + 'static,
{
    type Error = std::io::Error;

    fn send(
        &mut self,
        item: TxJsonRpcMessage<RoleServer>,
    ) -> impl Future<Output = Result<(), Self::Error>> + Send + 'static {
        let delivery = match &item {
            JsonRpcMessage::Response(response) => self.deliveries.write(&response.id),
            JsonRpcMessage::Error(error) => error
                .id
                .as_ref()
                .and_then(|request_id| self.deliveries.write(request_id)),
            JsonRpcMessage::Request(_) | JsonRpcMessage::Notification(_) => None,
        };
        let send = self.inner.send(item);
        async move {
            let result = send.await;
            if let Some(delivery) = delivery {
                if result.is_ok() {
                    delivery.delivered();
                } else {
                    delivery.unclaimed();
                }
            }
            result
        }
    }

    async fn receive(&mut self) -> Option<RxJsonRpcMessage<RoleServer>> {
        let mut message = self.inner.receive().await;
        match &mut message {
            Some(JsonRpcMessage::Request(request)) => {
                // Stdout bytes can reach the client before the current-thread
                // runtime resumes the write future to settle response ownership.
                // Do not dispatch a causally later request through that interval.
                self.deliveries.wait_for_writes().await;
                let call = self.deliveries.start(request.id.clone());
                request.request.extensions_mut().insert(call);
            }
            Some(JsonRpcMessage::Notification(notification)) => {
                if let ClientNotification::CancelledNotification(cancelled) =
                    &notification.notification
                    && let Some(request_id) = &cancelled.params.request_id
                {
                    self.deliveries.cancel(request_id);
                }
            }
            Some(JsonRpcMessage::Response(_) | JsonRpcMessage::Error(_)) => {}
            None => self.deliveries.close(),
        }
        message
    }

    fn close(&mut self) -> impl Future<Output = Result<(), Self::Error>> + Send {
        self.deliveries.close();
        self.inner.close()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request_id(value: i64) -> RequestId {
        RequestId::Number(value)
    }

    fn current_call(
        deliveries: &ResponseDeliveries,
        request_id: &RequestId,
    ) -> Option<ResponseDeliveryCall> {
        deliveries.lock().active.get(request_id).cloned()
    }

    fn is_active(call: &ResponseDeliveryCall) -> bool {
        call.lock().active
    }

    #[test]
    fn keeps_call_registered_until_response_write_finishes() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        assert!(current_call(&deliveries, &request_id).is_some_and(|current| current.is(&call)));
        assert!(is_active(&call));

        write.delivered();

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));
    }

    #[test]
    fn old_write_cannot_remove_reused_request_id() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let old_call = deliveries.start(request_id.clone());
        let old_write = deliveries.write(&request_id).unwrap();
        let new_call = deliveries.start(request_id.clone());

        assert!(!is_active(&old_call));
        assert!(
            current_call(&deliveries, &request_id).is_some_and(|current| current.is(&new_call))
        );

        old_write.delivered();

        assert!(
            current_call(&deliveries, &request_id).is_some_and(|current| current.is(&new_call))
        );
        assert!(is_active(&new_call));
    }

    #[test]
    fn cancellation_wins_over_in_flight_write() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        deliveries.cancel(&request_id);

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));

        write.delivered();
        assert!(current_call(&deliveries, &request_id).is_none());
    }

    #[tokio::test]
    async fn cancelled_receive_retains_request_waiting_for_response_write() {
        let deliveries = ResponseDeliveries::default();
        deliveries.lock().pending_writes = 1;
        let input =
            std::io::Cursor::new(b"{\"jsonrpc\":\"2.0\",\"method\":\"ping\",\"id\":1}\n".to_vec());
        let mut transport = ServerTransport::new(input, tokio::io::sink(), deliveries.clone());

        let mut receive = Box::pin(transport.receive());
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());
        assert!(std::future::Future::poll(receive.as_mut(), &mut context).is_pending());
        drop(receive);

        deliveries.settle_write();
        let Some(JsonRpcMessage::Request(request)) = transport.receive().await else {
            panic!("cancelled receive should retain its parsed request");
        };
        assert_eq!(request.id, request_id(1));
    }

    #[test]
    fn failed_write_releases_current_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        write.unclaimed();

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));
    }

    #[test]
    fn dropped_write_releases_current_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        drop(write);

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));
    }

    #[test]
    fn dropped_write_releases_only_its_own_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let old_call = deliveries.start(request_id.clone());
        let old_write = deliveries.write(&request_id).unwrap();
        let new_call = deliveries.start(request_id.clone());

        drop(old_write);

        assert!(!is_active(&old_call));
        assert!(
            current_call(&deliveries, &request_id).is_some_and(|current| current.is(&new_call))
        );
    }

    #[test]
    fn close_releases_all_active_calls() {
        let deliveries = ResponseDeliveries::default();
        let first = deliveries.start(request_id(1));
        let second = deliveries.start(request_id(2));

        deliveries.close();

        assert!(deliveries.lock().active.is_empty());
        assert!(!is_active(&first));
        assert!(!is_active(&second));

        let after_close = deliveries.start(request_id(3));
        assert!(!is_active(&after_close));
        assert!(deliveries.lock().active.is_empty());
    }
}
