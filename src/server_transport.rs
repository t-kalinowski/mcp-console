use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Arc, Mutex};

use rmcp::RoleServer;
use rmcp::model::{ClientNotification, ClientRequest, JsonRpcMessage, RequestId};
use rmcp::service::{RxJsonRpcMessage, TxJsonRpcMessage};
use rmcp::transport::{Transport, async_rw::AsyncRwTransport};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::Notify;

use crate::worker_client::ResponseDelivery;

#[derive(Clone, Default)]
pub(crate) struct ResponseDeliveries {
    state: Arc<Mutex<ResponseDeliveryState>>,
    write_finished: Arc<Notify>,
    transport_closed: Arc<Notify>,
}

#[derive(Clone)]
pub(crate) struct ResponseDeliveryAdmission(Arc<ResponseDeliveryAdmissionState>);

#[derive(Clone)]
pub(crate) struct ResponseDeliveryCall(Arc<Mutex<ResponseDeliveryCallState>>);

#[derive(Default)]
struct ResponseDeliveryState {
    active: HashMap<RequestId, ResponseDeliveryCall>,
    /// Delivery-tracked response-write futures registered before they run on the service task.
    pending_writes: usize,
    /// Tail of the wire-order chain, retained across cancellation gaps.
    admission_tail: Option<Arc<ResponseDeliveryAdmissionNode>>,
    closed: bool,
}

struct ResponseDeliveryAdmissionState {
    deliveries: ResponseDeliveries,
    node: Mutex<Option<Arc<ResponseDeliveryAdmissionNode>>>,
}

struct ResponseDeliveryAdmissionNode {
    state: AtomicU8,
    /// A skipped token retains this dependency so its successor cannot overtake it.
    predecessor: Option<Arc<ResponseDeliveryAdmissionNode>>,
    changed: Notify,
}

struct ResponseDeliveryCallState {
    active: bool,
    delivery: Option<ResponseDelivery>,
    admission: Option<Arc<ResponseDeliveryAdmissionNode>>,
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
    #[cfg(test)]
    fn start(&self, request_id: RequestId) -> ResponseDeliveryCall {
        let (call, replaced) = {
            let mut state = self.lock();
            Self::start_call(&mut state, request_id, None)
        };
        if let Some(replaced) = replaced {
            replaced.unclaimed();
        }
        call
    }

    /// Reserves transport order without retaining the request message.
    fn reserve(&self) -> ResponseDeliveryAdmission {
        let node = {
            let mut state = self.lock();
            let predecessor = state
                .admission_tail
                .take()
                .and_then(|tail| tail.unresolved());
            if state.closed || (state.pending_writes == 0 && predecessor.is_none()) {
                None
            } else {
                let node = Arc::new(ResponseDeliveryAdmissionNode::new(predecessor.clone()));
                state.admission_tail = Some(Arc::clone(&node));
                Some(node)
            }
        };
        ResponseDeliveryAdmission(Arc::new(ResponseDeliveryAdmissionState {
            deliveries: self.clone(),
            node: Mutex::new(node),
        }))
    }

    fn start_call(
        state: &mut ResponseDeliveryState,
        request_id: RequestId,
        admission: Option<Arc<ResponseDeliveryAdmissionNode>>,
    ) -> (ResponseDeliveryCall, Option<ResponseDelivery>) {
        let call = ResponseDeliveryCall::new(!state.closed, admission);
        let replaced = if state.closed {
            None
        } else {
            state.active.insert(request_id, call.clone())
        };
        let delivery = replaced.and_then(|replaced| replaced.finish());
        Self::prune_admission_tail(state);
        (call, delivery)
    }

    pub(crate) fn cancel(&self, request_id: &RequestId) {
        let delivery = {
            let mut state = self.lock();
            let delivery = state
                .active
                .remove(request_id)
                .and_then(|call| call.finish());
            Self::prune_admission_tail(&mut state);
            delivery
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
            let deliveries = state
                .active
                .drain()
                .filter_map(|(_, call)| call.finish())
                .collect::<Vec<_>>();
            state.admission_tail = None;
            deliveries
        };
        self.transport_closed.notify_waiters();
        self.write_finished.notify_waiters();
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
        let delivery = state
            .active
            .remove(request_id)
            .and_then(|call| call.finish());
        Self::prune_admission_tail(&mut state);
        delivery
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
        self.write_finished.notify_waiters();
    }

    async fn wait_for_close(&self) {
        loop {
            let transport_closed = self.transport_closed.notified();
            if self.lock().closed {
                return;
            }
            transport_closed.await;
        }
    }

    fn prune_admission_tail(state: &mut ResponseDeliveryState) {
        state.admission_tail = state
            .admission_tail
            .take()
            .and_then(|tail| tail.unresolved());
    }

    fn prune_tail(&self) {
        Self::prune_admission_tail(&mut self.lock());
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl ResponseDeliveryAdmission {
    /// Registers the call when every earlier gated call and response write has settled.
    pub(crate) async fn admit(self, request_id: RequestId) -> Option<ResponseDeliveryCall> {
        let node = self.0.node();
        let queued = node.is_some();
        if let Some(node) = node {
            tokio::select! {
                _ = node.wait_for_predecessor() => {}
                _ = self.0.deliveries.wait_for_close() => return None,
            }
        }
        loop {
            let write_finished = self.0.deliveries.write_finished.notified();
            let admission = {
                let mut state = self.0.deliveries.lock();
                if state.closed {
                    return None;
                }
                if !queued || state.pending_writes == 0 {
                    let node = queued.then(|| {
                        self.0
                            .take_node()
                            .expect("queued admission must retain its node")
                    });
                    Some(ResponseDeliveries::start_call(
                        &mut state,
                        request_id.clone(),
                        node,
                    ))
                } else {
                    None
                }
            };
            if let Some((call, replaced)) = admission {
                if let Some(replaced) = replaced {
                    replaced.unclaimed();
                }
                return Some(call);
            }
            write_finished.await;
        }
    }
}

impl ResponseDeliveryAdmissionState {
    fn node(&self) -> Option<Arc<ResponseDeliveryAdmissionNode>> {
        self.lock_node().clone()
    }

    fn take_node(&self) -> Option<Arc<ResponseDeliveryAdmissionNode>> {
        self.lock_node().take()
    }

    fn lock_node(&self) -> std::sync::MutexGuard<'_, Option<Arc<ResponseDeliveryAdmissionNode>>> {
        self.node
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Drop for ResponseDeliveryAdmissionState {
    fn drop(&mut self) {
        if let Some(node) = self
            .node
            .get_mut()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
        {
            node.skip();
            self.deliveries.prune_tail();
        }
    }
}

impl ResponseDeliveryAdmissionNode {
    const PENDING: u8 = 0;
    const FINISHED: u8 = 1;
    const SKIPPED: u8 = 2;

    fn new(predecessor: Option<Arc<Self>>) -> Self {
        Self {
            state: AtomicU8::new(Self::PENDING),
            predecessor,
            changed: Notify::new(),
        }
    }

    fn finish(&self) {
        if self
            .state
            .compare_exchange(
                Self::PENDING,
                Self::FINISHED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            self.changed.notify_waiters();
        }
    }

    fn skip(&self) {
        if self
            .state
            .compare_exchange(
                Self::PENDING,
                Self::SKIPPED,
                Ordering::AcqRel,
                Ordering::Acquire,
            )
            .is_ok()
        {
            self.changed.notify_waiters();
        }
    }

    /// Discards skipped nodes while retaining their first unfinished dependency.
    fn unresolved(self: &Arc<Self>) -> Option<Arc<Self>> {
        let mut current = Arc::clone(self);
        loop {
            match current.state.load(Ordering::Acquire) {
                Self::FINISHED => return None,
                Self::SKIPPED => {
                    let predecessor = current.predecessor.as_ref()?;
                    current = Arc::clone(predecessor);
                }
                Self::PENDING => return Some(current),
                _ => unreachable!("unknown response admission state"),
            }
        }
    }

    async fn wait_for_predecessor(&self) {
        if let Some(predecessor) = self.predecessor.as_ref() {
            predecessor.wait().await;
        }
    }

    async fn wait(self: &Arc<Self>) {
        let mut current = Arc::clone(self);
        loop {
            let changed = current.changed.notified();
            match current.state.load(Ordering::Acquire) {
                Self::FINISHED => return,
                Self::SKIPPED => {
                    let predecessor = current.predecessor.clone();
                    drop(changed);
                    let Some(predecessor) = predecessor else {
                        return;
                    };
                    current = predecessor;
                }
                Self::PENDING => changed.await,
                _ => unreachable!("unknown response admission state"),
            }
        }
    }
}

impl ResponseDeliveryCall {
    fn new(active: bool, admission: Option<Arc<ResponseDeliveryAdmissionNode>>) -> Self {
        Self(Arc::new(Mutex::new(ResponseDeliveryCallState {
            active,
            delivery: None,
            admission,
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
        if let Some(admission) = state.admission.take() {
            admission.finish();
        }
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
        let deliveries = self.deliveries.clone();
        async move {
            // Preserve a ready final response after input EOF, but abandon a
            // blocked stdout write so transport shutdown remains bounded.
            let result = tokio::select! {
                biased;
                result = send => result,
                _ = deliveries.wait_for_close() => {
                    Err(std::io::ErrorKind::BrokenPipe.into())
                },
            };
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
                if let ClientRequest::CallToolRequest(call) = &mut request.request
                    && matches!(call.params.name.as_ref(), "send" | "session")
                {
                    call.extensions.insert(self.deliveries.reserve());
                }
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
            None => {
                self.deliveries.close();
            }
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
    use tokio::io::AsyncWriteExt;

    const PING_REQUEST: &[u8] = b"{\"jsonrpc\":\"2.0\",\"method\":\"ping\",\"id\":1}\n";
    const CANCEL_REQUEST_9: &[u8] = b"{\"jsonrpc\":\"2.0\",\"method\":\"notifications/cancelled\",\"params\":{\"requestId\":9}}\n";

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
    async fn response_gate_does_not_delay_cancellation_notifications() {
        let deliveries = ResponseDeliveries::default();
        deliveries.lock().pending_writes = 1;
        let cancelled = deliveries.start(request_id(9));
        let (mut input, read) = tokio::io::duplex(1024);
        input.write_all(PING_REQUEST).await.unwrap();
        input.write_all(CANCEL_REQUEST_9).await.unwrap();
        let mut transport = ServerTransport::new(read, tokio::io::sink(), deliveries);

        let first = {
            let mut receive = Box::pin(transport.receive());
            let mut context = std::task::Context::from_waker(std::task::Waker::noop());
            match std::future::Future::poll(receive.as_mut(), &mut context) {
                std::task::Poll::Ready(Some(message)) => message,
                _ => panic!("the gated request should reach rmcp immediately"),
            }
        };
        assert!(matches!(first, JsonRpcMessage::Request(_)));

        let second = {
            let mut receive = Box::pin(transport.receive());
            let mut context = std::task::Context::from_waker(std::task::Waker::noop());
            match std::future::Future::poll(receive.as_mut(), &mut context) {
                std::task::Poll::Ready(Some(message)) => message,
                _ => panic!("cancellation should not wait for the response write"),
            }
        };
        assert!(matches!(second, JsonRpcMessage::Notification(_)));
        assert!(!is_active(&cancelled));
    }

    #[tokio::test]
    async fn response_gate_skips_cancelled_calls_without_reordering() {
        let deliveries = ResponseDeliveries::default();
        deliveries.lock().pending_writes = 1;
        let first = deliveries.reserve();
        let cancelled = deliveries.reserve();
        drop(cancelled);
        let second = deliveries.reserve();
        let mut first = Box::pin(first.admit(request_id(1)));
        let mut second = Box::pin(second.admit(request_id(2)));
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());

        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        assert!(std::future::Future::poll(first.as_mut(), &mut context).is_pending());
        deliveries.settle_write();
        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        let first_call = match std::future::Future::poll(first.as_mut(), &mut context) {
            std::task::Poll::Ready(Some(call)) => call,
            _ => panic!("the first reserved call should be admitted"),
        };

        let first_write = deliveries.write(&request_id(1)).unwrap();
        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        first_write.delivered();
        assert!(matches!(
            std::future::Future::poll(second.as_mut(), &mut context),
            std::task::Poll::Ready(Some(_))
        ));
        assert!(!is_active(&first_call));
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
