use std::collections::HashMap;
use std::sync::atomic::{AtomicU8, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use rmcp::RoleServer;
use rmcp::model::{ClientNotification, ClientRequest, JsonRpcMessage, RequestId};
use rmcp::service::{RxJsonRpcMessage, TxJsonRpcMessage};
use rmcp::transport::{Transport, async_rw::AsyncRwTransport};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::sync::Notify;

use crate::worker_client::ResponseDelivery;

// A blocked stdout cannot carry a correlated overload response. Closing the
// transport at this bound keeps rmcp's detached handler backlog finite.
const MAX_RESPONSE_GATED_CALLS: usize = 64;

#[derive(Clone, Default)]
pub(crate) struct ResponseDeliveries {
    state: Arc<Mutex<ResponseDeliveryState>>,
    gate_changed: Arc<Notify>,
    transport_closed: Arc<Notify>,
    /// Counts live admission nodes, including terminal nodes retained by a successor.
    admission_count: Arc<AtomicUsize>,
}

#[derive(Clone)]
pub(crate) struct ResponseDeliveryAdmission(Arc<ResponseDeliveryAdmissionState>);

#[derive(Clone)]
pub(crate) struct ResponseDeliveryCall {
    request_id: RequestId,
    state: Arc<Mutex<ResponseDeliveryCallState>>,
}

pub(crate) struct ResponseDeliveryOperation {
    deliveries: ResponseDeliveries,
    request_id: RequestId,
    call: ResponseDeliveryCall,
    completed: bool,
}

pub(crate) enum ResponseDeliveryAdmissionError {
    Cancelled,
    Closed,
}

#[derive(Default)]
struct ResponseDeliveryState {
    active: HashMap<RequestId, ResponseDeliveryCall>,
    /// Only node-backed reservations enter this map, so the node cap also bounds it.
    pending: HashMap<RequestId, u64>,
    next_admission_token: u64,
    /// Console response-write futures registered before they run on the service task.
    pending_writes: usize,
    /// Last waiting member of the wire-order chain, retained across cancellation gaps.
    admission_tail: Option<Arc<ResponseDeliveryAdmissionNode>>,
    closed: bool,
}

struct ResponseDeliveryAdmissionState {
    deliveries: ResponseDeliveries,
    request_id: RequestId,
    token: Option<u64>,
    node: Mutex<Option<Arc<ResponseDeliveryAdmissionNode>>>,
}

struct ResponseDeliveryAdmissionNode {
    state: AtomicU8,
    /// Cleared only after the dependency settles so cancellation cannot break the chain.
    predecessor: Mutex<Option<Arc<ResponseDeliveryAdmissionNode>>>,
    changed: Notify,
    admission_count: Arc<AtomicUsize>,
}

struct ResponseDeliveryCallState {
    active: bool,
    operation_finished: bool,
    delivery: Option<ResponseDelivery>,
    admission: Option<Arc<ResponseDeliveryAdmissionNode>>,
}

/// Keeps one console request registered until its response write finishes.
struct ResponseDeliveryWrite {
    deliveries: ResponseDeliveries,
    request_id: RequestId,
    call: ResponseDeliveryCall,
    finished: bool,
}

impl ResponseDeliveries {
    #[cfg(test)]
    fn start_response(&self, request_id: RequestId) -> ResponseDeliveryCall {
        self.admission_count.fetch_add(1, Ordering::AcqRel);
        let admission = Arc::new(ResponseDeliveryAdmissionNode::new(
            None,
            Arc::clone(&self.admission_count),
        ));
        admission.admit();
        let (call, replaced) = {
            let mut state = self.lock();
            Self::start_call(&mut state, request_id, Some(admission))
        };
        if let Some(replaced) = replaced {
            replaced.unclaimed();
        }
        {
            let mut state = call.lock();
            state.operation_finished = true;
            if !state.active {
                ResponseDeliveryCall::finish_admission(&mut state);
            }
        }
        call
    }

    /// Reserves transport order without retaining the request message.
    fn reserve(&self, request_id: RequestId) -> Option<ResponseDeliveryAdmission> {
        let (token, node) = {
            let mut state = self.lock();
            Self::prune_admission_tail(&mut state);
            if state.closed {
                return None;
            }
            let node = if !Self::reservation_gate_active(&state) {
                None
            } else {
                if self.admission_count.load(Ordering::Acquire) >= MAX_RESPONSE_GATED_CALLS {
                    return None;
                }
                self.admission_count.fetch_add(1, Ordering::AcqRel);
                let predecessor = state.admission_tail.clone();
                let node = Arc::new(ResponseDeliveryAdmissionNode::new(
                    predecessor,
                    Arc::clone(&self.admission_count),
                ));
                state.admission_tail = Some(Arc::clone(&node));
                Some(node)
            };
            let token = node.as_ref().map(|_| {
                let token = state.next_admission_token;
                state.next_admission_token = token
                    .checked_add(1)
                    .expect("response admission token space exhausted");
                state.pending.insert(request_id.clone(), token);
                token
            });
            (token, node)
        };
        let admission = ResponseDeliveryAdmission(Arc::new(ResponseDeliveryAdmissionState {
            deliveries: self.clone(),
            request_id: request_id.clone(),
            token,
            node: Mutex::new(node),
        }));
        crate::test_control::emit(&request_id, "operation_accepted", "transport");
        self.gate_changed.notify_waiters();
        Some(admission)
    }

    fn start_call(
        state: &mut ResponseDeliveryState,
        request_id: RequestId,
        admission: Option<Arc<ResponseDeliveryAdmissionNode>>,
    ) -> (ResponseDeliveryCall, Option<ResponseDelivery>) {
        let call = ResponseDeliveryCall::new(request_id.clone(), !state.closed, admission);
        let replaced = if state.closed {
            None
        } else {
            state.active.insert(request_id, call.clone())
        };
        let delivery = replaced.and_then(|replaced| replaced.abandon());
        Self::prune_admission_tail(state);
        (call, delivery)
    }

    pub(crate) fn cancel(&self, request_id: &RequestId) {
        let (cancellation, delivery) = {
            let mut state = self.lock();
            // A pending reuse of the ID is newer than an active call whose
            // already-visible response write has not settled yet.
            let pending = state.pending.remove(request_id).is_some();
            let active = (!pending)
                .then(|| state.active.get(request_id).cloned())
                .flatten();
            let delivery = active.as_ref().and_then(|call| {
                let (remove, delivery) = call.cancel();
                if remove {
                    state.active.remove(request_id);
                }
                delivery
            });
            Self::prune_admission_tail(&mut state);
            let cancellation = if pending {
                Some("cancellation_before_admission")
            } else if active.is_some() {
                Some("cancellation_after_admission")
            } else {
                None
            };
            (cancellation, delivery)
        };
        if let Some(kind) = cancellation {
            crate::test_control::emit(request_id, kind, "transport");
        }
        if let Some(delivery) = delivery {
            delivery.unclaimed();
        }
        self.gate_changed.notify_waiters();
    }

    pub(crate) fn register(&self, call: &ResponseDeliveryCall, delivery: ResponseDelivery) {
        let rejected = {
            let state = self.lock();
            let current = state.active.values().find(|current| current.is(call));
            if current.is_some() {
                call.register(delivery).err()
            } else {
                Some(delivery)
            }
        };
        if let Some(delivery) = rejected {
            delivery.unclaimed();
        }
    }

    fn finish_operation(
        &self,
        request_id: &RequestId,
        expected: &ResponseDeliveryCall,
        completed: bool,
    ) {
        let delivery = {
            let mut state = self.lock();
            let Some(current) = state.active.get(request_id) else {
                return;
            };
            if !current.is(expected) {
                return;
            }
            let (remove, delivery) = if completed {
                expected.complete_operation()
            } else {
                (true, expected.abandon())
            };
            if remove {
                state.active.remove(request_id);
            }
            Self::prune_admission_tail(&mut state);
            delivery
        };
        crate::test_control::emit(request_id, "operation_stopped", "transport");
        if let Some(delivery) = delivery {
            delivery.unclaimed();
        }
        self.gate_changed.notify_waiters();
    }

    fn write(&self, request_id: &RequestId) -> Option<ResponseDeliveryWrite> {
        let call = {
            let mut state = self.lock();
            let call = state.active.get(request_id).cloned()?;
            call.begin_write();
            state.pending_writes += 1;
            call
        };
        Some(ResponseDeliveryWrite {
            deliveries: self.clone(),
            request_id: request_id.clone(),
            call,
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
                .filter_map(|(_, call)| call.abandon())
                .collect::<Vec<_>>();
            state.pending.clear();
            state.admission_tail = None;
            deliveries
        };
        self.transport_closed.notify_waiters();
        self.gate_changed.notify_waiters();
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
            .and_then(|call| call.finish_write());
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
        self.gate_changed.notify_waiters();
    }

    fn response_gate_active(state: &ResponseDeliveryState) -> bool {
        state.pending_writes > 0
            || state
                .active
                .values()
                .any(ResponseDeliveryCall::blocks_new_admissions)
    }

    fn reservation_gate_active(state: &ResponseDeliveryState) -> bool {
        Self::response_gate_active(state) || state.admission_tail.is_some()
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
        state.admission_tail = state.admission_tail.take().and_then(|tail| tail.waiting());
    }

    fn owns_pending(
        state: &ResponseDeliveryState,
        admission: &ResponseDeliveryAdmissionState,
    ) -> bool {
        admission.token.is_none_or(|expected| {
            state
                .pending
                .get(&admission.request_id)
                .is_some_and(|token| *token == expected)
        })
    }

    fn unregister_pending(&self, request_id: &RequestId, token: u64) {
        {
            let mut state = self.lock();
            if state
                .pending
                .get(request_id)
                .is_some_and(|current| *current == token)
            {
                state.pending.remove(request_id);
            }
            Self::prune_admission_tail(&mut state);
        }
        self.gate_changed.notify_waiters();
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl ResponseDeliveryAdmission {
    /// Registers the call when every earlier gated call and response write has settled.
    pub(crate) async fn admit(
        self,
    ) -> Result<(ResponseDeliveryCall, ResponseDeliveryOperation), ResponseDeliveryAdmissionError>
    {
        let node = self.0.node();
        let queued = node.is_some();
        if queued {
            crate::test_control::emit(&self.0.request_id, "operation_waiting", "transport");
        }
        if let Some(node) = &node {
            loop {
                let gate_changed = self.0.deliveries.gate_changed.notified();
                {
                    let state = self.0.deliveries.lock();
                    if state.closed {
                        return Err(ResponseDeliveryAdmissionError::Closed);
                    }
                    if !ResponseDeliveries::owns_pending(&state, &self.0) {
                        return Err(ResponseDeliveryAdmissionError::Cancelled);
                    }
                }
                tokio::select! {
                    _ = node.wait_for_predecessor() => break,
                    _ = gate_changed => {}
                }
            }
        }
        loop {
            let gate_changed = self.0.deliveries.gate_changed.notified();
            let admission = {
                let mut state = self.0.deliveries.lock();
                if state.closed {
                    return Err(ResponseDeliveryAdmissionError::Closed);
                }
                if !ResponseDeliveries::owns_pending(&state, &self.0) {
                    return Err(ResponseDeliveryAdmissionError::Cancelled);
                }
                if !queued || !ResponseDeliveries::response_gate_active(&state) {
                    if self.0.token.is_some() {
                        state.pending.remove(&self.0.request_id);
                    }
                    let node = node.as_ref().map(|_| {
                        self.0
                            .take_node()
                            .expect("queued admission must retain its node")
                    });
                    if let Some(node) = &node {
                        node.admit();
                    }
                    Some(ResponseDeliveries::start_call(
                        &mut state,
                        self.0.request_id.clone(),
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
                self.0.deliveries.gate_changed.notify_waiters();
                let operation = ResponseDeliveryOperation {
                    deliveries: self.0.deliveries.clone(),
                    request_id: self.0.request_id.clone(),
                    call: call.clone(),
                    completed: false,
                };
                crate::test_control::emit(&self.0.request_id, "operation_admitted", "transport");
                return Ok((call, operation));
            }
            gate_changed.await;
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
        }
        if let Some(token) = self.token {
            self.deliveries.unregister_pending(&self.request_id, token);
        }
    }
}

impl ResponseDeliveryAdmissionNode {
    const WAITING: u8 = 0;
    const ADMITTED: u8 = 1;
    const FINISHED: u8 = 2;
    const SKIPPED: u8 = 3;

    fn new(predecessor: Option<Arc<Self>>, admission_count: Arc<AtomicUsize>) -> Self {
        Self {
            state: AtomicU8::new(Self::WAITING),
            predecessor: Mutex::new(predecessor),
            changed: Notify::new(),
            admission_count,
        }
    }

    fn admit(&self) {
        self.transition(Self::WAITING, Self::ADMITTED);
    }

    fn finish(&self) {
        self.transition(Self::ADMITTED, Self::FINISHED);
    }

    fn skip(&self) {
        self.transition(Self::WAITING, Self::SKIPPED);
    }

    fn transition(&self, expected: u8, next: u8) {
        if self
            .state
            .compare_exchange(expected, next, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            self.changed.notify_waiters();
        }
    }

    /// Finds the last waiting node without restoring an admitted predecessor as the tail.
    fn waiting(self: &Arc<Self>) -> Option<Arc<Self>> {
        let mut current = Arc::clone(self);
        loop {
            match current.state.load(Ordering::Acquire) {
                Self::WAITING => return Some(current),
                Self::ADMITTED | Self::FINISHED => return None,
                Self::SKIPPED => {
                    current = current.predecessor()?;
                }
                _ => unreachable!("unknown response admission state"),
            }
        }
    }

    async fn wait_for_predecessor(&self) {
        if let Some(predecessor) = self.predecessor() {
            predecessor.wait().await;
            let mut current = self.lock_predecessor();
            if current
                .as_ref()
                .is_some_and(|current| Arc::ptr_eq(current, &predecessor))
            {
                *current = None;
            }
        }
    }

    async fn wait(self: &Arc<Self>) {
        let mut current = Arc::clone(self);
        loop {
            let changed = current.changed.notified();
            match current.state.load(Ordering::Acquire) {
                Self::FINISHED => return,
                Self::SKIPPED => {
                    let predecessor = current.predecessor();
                    drop(changed);
                    let Some(predecessor) = predecessor else {
                        return;
                    };
                    current = predecessor;
                }
                Self::WAITING | Self::ADMITTED => changed.await,
                _ => unreachable!("unknown response admission state"),
            }
        }
    }

    fn predecessor(&self) -> Option<Arc<Self>> {
        self.lock_predecessor().clone()
    }

    fn lock_predecessor(&self) -> std::sync::MutexGuard<'_, Option<Arc<Self>>> {
        self.predecessor
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Drop for ResponseDeliveryAdmissionNode {
    fn drop(&mut self) {
        let previous = self.admission_count.fetch_sub(1, Ordering::AcqRel);
        assert!(previous > 0, "a reserved admission must occupy one slot");
    }
}

impl ResponseDeliveryCall {
    fn new(
        request_id: RequestId,
        active: bool,
        admission: Option<Arc<ResponseDeliveryAdmissionNode>>,
    ) -> Self {
        Self {
            request_id,
            state: Arc::new(Mutex::new(ResponseDeliveryCallState {
                active,
                operation_finished: false,
                delivery: None,
                admission,
            })),
        }
    }

    fn register(&self, delivery: ResponseDelivery) -> Result<(), ResponseDelivery> {
        let mut state = self.lock();
        if !state.active {
            return Err(delivery);
        }
        assert!(
            !state.operation_finished,
            "response delivery must register before operation completion"
        );
        assert!(
            state.delivery.replace(delivery).is_none(),
            "a response can register only one delivery acknowledgment"
        );
        Ok(())
    }

    fn cancel(&self) -> (bool, Option<ResponseDelivery>) {
        let mut state = self.lock();
        state.active = false;
        let delivery = state.delivery.take();
        if state.operation_finished {
            Self::finish_admission(&mut state);
            (true, delivery)
        } else {
            (false, delivery)
        }
    }

    fn complete_operation(&self) -> (bool, Option<ResponseDelivery>) {
        let mut state = self.lock();
        assert!(
            !state.operation_finished,
            "a response operation can complete only once"
        );
        state.operation_finished = true;
        if !state.active {
            Self::finish_admission(&mut state);
            return (true, state.delivery.take());
        }
        (false, None)
    }

    fn abandon(&self) -> Option<ResponseDelivery> {
        let mut state = self.lock();
        state.active = false;
        state.operation_finished = true;
        Self::finish_admission(&mut state);
        state.delivery.take()
    }

    fn finish_write(&self) -> Option<ResponseDelivery> {
        let mut state = self.lock();
        state.active = false;
        Self::finish_admission(&mut state);
        state.delivery.take()
    }

    fn finish_admission(state: &mut ResponseDeliveryCallState) {
        if let Some(admission) = state.admission.take() {
            admission.finish();
        }
    }

    fn is(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.state, &other.state)
    }

    fn blocks_new_admissions(&self) -> bool {
        let state = self.lock();
        state.operation_finished
            || state.delivery.is_some()
            || (state.admission.is_some() && !state.active)
    }

    fn begin_write(&self) {
        let state = self.lock();
        assert!(
            state.operation_finished,
            "response writing must follow operation completion"
        );
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryCallState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub(crate) fn test_operation(&self) -> crate::test_control::Operation {
        crate::test_control::Operation::new(self.request_id.clone())
    }
}

impl ResponseDeliveryOperation {
    pub(crate) fn complete(mut self) {
        self.completed = true;
        self.deliveries
            .finish_operation(&self.request_id, &self.call, true);
    }
}

impl Drop for ResponseDeliveryOperation {
    fn drop(&mut self) {
        if !self.completed {
            self.deliveries
                .finish_operation(&self.request_id, &self.call, false);
        }
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
        self.deliveries.settle_write();
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
                result = async {
                    if let Some(delivery) = delivery.as_ref() {
                        crate::test_control::emit(
                            &delivery.request_id,
                            "response_write_started",
                            "transport",
                        );
                        crate::test_control::wait_for_response_gate(&delivery.request_id).await;
                    }
                    send.await
                } => result,
                _ = deliveries.wait_for_close() => {
                    Err(std::io::ErrorKind::BrokenPipe.into())
                },
            };
            if let Some(delivery) = delivery {
                let request_id = delivery.request_id.clone();
                if result.is_ok() {
                    crate::test_control::emit(&request_id, "response_write_completed", "transport");
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
                    && call.params.name.as_ref() == "send"
                {
                    let Some(admission) = self.deliveries.reserve(request.id.clone()) else {
                        // Under stdout backpressure another response is not reliable,
                        // so overload retires the connection and its worker session.
                        crate::test_control::emit(&request.id, "transport_closed", "transport");
                        self.deliveries.close();
                        return None;
                    };
                    call.extensions.insert(admission);
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
                crate::test_control::emit(&0, "transport_closed", "transport");
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
        let call = deliveries.start_response(request_id.clone());
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
        let old_call = deliveries.start_response(request_id.clone());
        let old_write = deliveries.write(&request_id).unwrap();
        let new_call = deliveries.start_response(request_id.clone());

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
        let call = deliveries.start_response(request_id.clone());
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
        let cancelled = deliveries.start_response(request_id(9));
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
        let first = deliveries.reserve(request_id(1)).unwrap();
        let cancelled = deliveries.reserve(request_id(9)).unwrap();
        drop(cancelled);
        let second = deliveries.reserve(request_id(2)).unwrap();
        let mut first = Box::pin(first.admit());
        let mut second = Box::pin(second.admit());
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());

        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        assert!(std::future::Future::poll(first.as_mut(), &mut context).is_pending());
        deliveries.settle_write();
        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        let (first_call, first_operation) =
            match std::future::Future::poll(first.as_mut(), &mut context) {
                std::task::Poll::Ready(Ok(call)) => call,
                _ => panic!("the first reserved call should be admitted"),
            };

        first_operation.complete();
        let first_write = deliveries.write(&request_id(1)).unwrap();
        assert!(std::future::Future::poll(second.as_mut(), &mut context).is_pending());
        first_write.delivered();
        assert!(matches!(
            std::future::Future::poll(second.as_mut(), &mut context),
            std::task::Poll::Ready(Ok((_call, _operation)))
        ));
        assert!(!is_active(&first_call));
    }

    #[tokio::test]
    async fn cancelled_pending_admission_cannot_start_and_does_not_skip_successor() {
        let deliveries = ResponseDeliveries::default();
        let blocker_id = request_id(0);
        deliveries.start_response(blocker_id.clone());
        let blocker_write = deliveries.write(&blocker_id).unwrap();
        let cancelled_id = request_id(1);
        let successor_id = request_id(2);
        let cancelled = deliveries.reserve(cancelled_id.clone()).unwrap();
        let successor = deliveries.reserve(successor_id).unwrap();
        let mut cancelled = Box::pin(cancelled.admit());
        let mut successor = Box::pin(successor.admit());
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());

        assert!(std::future::Future::poll(cancelled.as_mut(), &mut context).is_pending());
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());

        deliveries.cancel(&cancelled_id);
        assert!(matches!(
            std::future::Future::poll(cancelled.as_mut(), &mut context),
            std::task::Poll::Ready(Err(ResponseDeliveryAdmissionError::Cancelled))
        ));
        assert!(!deliveries.lock().pending.contains_key(&cancelled_id));
        assert!(current_call(&deliveries, &cancelled_id).is_none());
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());

        blocker_write.delivered();
        assert!(matches!(
            std::future::Future::poll(successor.as_mut(), &mut context),
            std::task::Poll::Ready(Ok((_call, _operation)))
        ));
    }

    #[tokio::test]
    async fn cancelled_admitted_call_retains_order_until_operation_stops() {
        let deliveries = ResponseDeliveries::default();
        let blocker_id = request_id(0);
        deliveries.start_response(blocker_id.clone());
        let blocker_write = deliveries.write(&blocker_id).unwrap();
        let first_id = request_id(1);
        let successor_id = request_id(2);
        let first = deliveries.reserve(first_id.clone()).unwrap();
        let successor = deliveries.reserve(successor_id).unwrap();
        let mut first = Box::pin(first.admit());
        let mut successor = Box::pin(successor.admit());
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());

        assert!(std::future::Future::poll(first.as_mut(), &mut context).is_pending());
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());
        blocker_write.delivered();
        let (first_call, first_operation) =
            match std::future::Future::poll(first.as_mut(), &mut context) {
                std::task::Poll::Ready(Ok(admitted)) => admitted,
                _ => panic!("the first reserved call should be admitted"),
            };
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());

        deliveries.cancel(&first_id);
        assert!(current_call(&deliveries, &first_id).is_some());
        assert!(!is_active(&first_call));
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());

        drop(first_operation);
        assert!(current_call(&deliveries, &first_id).is_none());
        assert!(matches!(
            std::future::Future::poll(successor.as_mut(), &mut context),
            std::task::Poll::Ready(Ok((_call, _operation)))
        ));
    }

    #[tokio::test]
    async fn completed_operation_holds_order_until_response_write_settles() {
        let deliveries = ResponseDeliveries::default();
        let blocker_id = request_id(0);
        deliveries.start_response(blocker_id.clone());
        let blocker_write = deliveries.write(&blocker_id).unwrap();
        let first_id = request_id(1);
        let successor_id = request_id(2);
        let first = deliveries.reserve(first_id.clone()).unwrap();
        let successor = deliveries.reserve(successor_id).unwrap();
        let mut first = Box::pin(first.admit());
        let mut successor = Box::pin(successor.admit());
        let mut context = std::task::Context::from_waker(std::task::Waker::noop());

        assert!(std::future::Future::poll(first.as_mut(), &mut context).is_pending());
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());
        blocker_write.delivered();
        let (_first_call, first_operation) =
            match std::future::Future::poll(first.as_mut(), &mut context) {
                std::task::Poll::Ready(Ok(admitted)) => admitted,
                _ => panic!("the first reserved call should be admitted"),
            };

        first_operation.complete();
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());
        let first_write = deliveries.write(&first_id).unwrap();
        assert_eq!(deliveries.lock().pending_writes, 1);
        assert!(current_call(&deliveries, &first_id).is_some());
        assert!(std::future::Future::poll(successor.as_mut(), &mut context).is_pending());

        first_write.delivered();
        assert_eq!(deliveries.lock().pending_writes, 0);
        assert!(current_call(&deliveries, &first_id).is_none());
        assert!(matches!(
            std::future::Future::poll(successor.as_mut(), &mut context),
            std::task::Poll::Ready(Ok((_call, _operation)))
        ));
    }

    #[tokio::test]
    async fn completed_initial_operation_remains_registered_until_its_response_write() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let admission = deliveries.reserve(request_id.clone()).unwrap();
        let (call, operation) = match admission.admit().await {
            Ok(admitted) => admitted,
            Err(_) => panic!("the initial call should be admitted"),
        };

        operation.complete();
        assert!(current_call(&deliveries, &request_id).is_some_and(|current| current.is(&call)));
        assert!(call.blocks_new_admissions());

        let write = deliveries.write(&request_id).unwrap();
        assert_eq!(deliveries.lock().pending_writes, 1);
        write.delivered();

        assert_eq!(deliveries.lock().pending_writes, 0);
        assert!(current_call(&deliveries, &request_id).is_none());
    }

    #[test]
    fn failed_write_releases_current_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start_response(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        write.unclaimed();

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));
    }

    #[test]
    fn dropped_write_releases_current_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let call = deliveries.start_response(request_id.clone());
        let write = deliveries.write(&request_id).unwrap();

        drop(write);

        assert!(current_call(&deliveries, &request_id).is_none());
        assert!(!is_active(&call));
    }

    #[test]
    fn dropped_write_releases_only_its_own_call() {
        let deliveries = ResponseDeliveries::default();
        let request_id = request_id(1);
        let old_call = deliveries.start_response(request_id.clone());
        let old_write = deliveries.write(&request_id).unwrap();
        let new_call = deliveries.start_response(request_id.clone());

        drop(old_write);

        assert!(!is_active(&old_call));
        assert!(
            current_call(&deliveries, &request_id).is_some_and(|current| current.is(&new_call))
        );
    }

    #[test]
    fn close_releases_all_active_calls() {
        let deliveries = ResponseDeliveries::default();
        let first = deliveries.start_response(request_id(1));
        let second = deliveries.start_response(request_id(2));

        deliveries.close();

        assert!(deliveries.lock().active.is_empty());
        assert!(!is_active(&first));
        assert!(!is_active(&second));

        let after_close = deliveries.start_response(request_id(3));
        assert!(!is_active(&after_close));
        assert!(deliveries.lock().active.is_empty());
    }
}
