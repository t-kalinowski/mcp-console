use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use rmcp::RoleServer;
use rmcp::model::{ClientNotification, GetExtensions, JsonRpcMessage, RequestId};
use rmcp::service::{RxJsonRpcMessage, TxJsonRpcMessage};
use rmcp::transport::{Transport, async_rw::AsyncRwTransport};
use tokio::io::{AsyncRead, AsyncWrite};

use crate::worker_client::ResponseDelivery;

#[derive(Clone, Default)]
pub(crate) struct ResponseDeliveries(Arc<Mutex<ResponseDeliveryState>>);

#[derive(Clone)]
pub(crate) struct ResponseDeliveryCall(Arc<Mutex<ResponseDeliveryCallState>>);

#[derive(Default)]
struct ResponseDeliveryState {
    active: HashMap<RequestId, ResponseDeliveryCall>,
    closed: bool,
}

struct ResponseDeliveryCallState {
    active: bool,
    delivery: Option<ResponseDelivery>,
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
        drop(state);
        if let Some(replaced) = replaced {
            replaced.finish();
        }
        call
    }

    pub(crate) fn cancel(&self, request_id: &RequestId) {
        let call = self.lock().active.remove(request_id);
        if let Some(call) = call {
            call.finish();
        }
    }

    fn take_for_send(&self, request_id: &RequestId) -> Option<ResponseDelivery> {
        self.lock()
            .active
            .remove(request_id)
            .and_then(|call| call.take_delivery())
    }

    fn close(&self) {
        let calls = {
            let mut state = self.lock();
            state.closed = true;
            state
                .active
                .drain()
                .map(|(_, call)| call)
                .collect::<Vec<_>>()
        };
        for call in calls {
            call.finish();
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryState> {
        self.0
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
            delivery.complete();
            return;
        }
        assert!(
            state.delivery.replace(delivery).is_none(),
            "a response can register only one delivery acknowledgment"
        );
    }

    fn take_delivery(&self) -> Option<ResponseDelivery> {
        let mut state = self.lock();
        state.active = false;
        state.delivery.take()
    }

    fn finish(&self) {
        if let Some(delivery) = self.take_delivery() {
            delivery.complete();
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, ResponseDeliveryCallState> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
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
            JsonRpcMessage::Response(response) => self.deliveries.take_for_send(&response.id),
            JsonRpcMessage::Error(error) => error
                .id
                .as_ref()
                .and_then(|request_id| self.deliveries.take_for_send(request_id)),
            JsonRpcMessage::Request(_) | JsonRpcMessage::Notification(_) => None,
        };
        let send = self.inner.send(item);
        async move {
            let result = send.await;
            if let Some(delivery) = delivery {
                delivery.complete();
            }
            result
        }
    }

    async fn receive(&mut self) -> Option<RxJsonRpcMessage<RoleServer>> {
        let mut message = self.inner.receive().await;
        match &mut message {
            Some(JsonRpcMessage::Request(request)) => {
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
