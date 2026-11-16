use crate::Xray;
use alloy_json_rpc::{RequestPacket, ResponsePacket, RpcError};
use alloy_transport::{TransportError, TransportErrorKind};
use std::{
    future::Future,
    io::Write,
    pin::Pin,
    task::{Context, Poll},
};
use tower::{Layer, Service};

#[derive(Clone)]
pub struct XrayLayer {
    xray: Xray,
    url: String,
    chain: String,
}
impl XrayLayer {
    pub fn new(xray: Xray, url: impl Into<String>, chain: impl Into<String>) -> Self {
        Self {
            xray,
            url: url.into(),
            chain: chain.into(),
        }
    }
}
#[derive(Clone)]
pub struct XrayService<S> {
    inner: S,
    layer: XrayLayer,
}
impl<S> Layer<S> for XrayLayer {
    type Service = XrayService<S>;
    fn layer(&self, inner: S) -> Self::Service {
        XrayService {
            inner,
            layer: self.clone(),
        }
    }
}

struct Capped {
    bytes: Vec<u8>,
    cap: usize,
}
impl Write for Capped {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if self.bytes.len() + bytes.len() > self.cap {
            return Err(std::io::Error::other("inspection limit"));
        }
        self.bytes.extend_from_slice(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn encode(value: &impl serde::Serialize, cap: usize) -> Option<Vec<u8>> {
    let mut out = Capped {
        bytes: Vec::new(),
        cap,
    };
    serde_json::to_writer(&mut out, value).ok()?;
    Some(out.bytes)
}

impl<S> Service<RequestPacket> for XrayService<S>
where
    S: Service<RequestPacket, Response = ResponsePacket, Error = TransportError>,
    S::Future: Send + 'static,
{
    type Response = ResponsePacket;
    type Error = TransportError;
    type Future = Pin<Box<dyn Future<Output = Result<ResponsePacket, TransportError>> + Send>>;
    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }
    fn call(&mut self, request: RequestPacket) -> Self::Future {
        let cap = self.layer.xray.options.max_body_bytes;
        let observation = encode(&request, cap).and_then(|body| {
            self.layer
                .xray
                .start(&self.layer.url, &self.layer.chain, &body)
        });
        let future = self.inner.call(request);
        Box::pin(async move {
            let mut pending = crate::Pending(observation);
            let result = future.await;
            if let Some(observation) = pending.0.take() {
                match &result {
                    Ok(response) => {
                        let body = match response {
                            ResponsePacket::Single(single) => encode(single, cap),
                            ResponsePacket::Batch(batch) => encode(batch, cap),
                        };
                        observation.finish(200, body.as_deref(), None);
                    }
                    Err(RpcError::Transport(TransportErrorKind::HttpError(error))) => {
                        observation.finish(error.status, None, None)
                    }
                    Err(RpcError::ErrorResp(error)) => {
                        let payload = serde_json::json!({"error":{"code":error.code,"message":error.message}});
                        observation.finish(200, encode(&payload, cap).as_deref(), None);
                    }
                    Err(RpcError::DeserError { text, .. }) => {
                        observation.finish(200, Some(text.as_bytes()), None)
                    }
                    Err(RpcError::NullResp) => observation.finish(200, Some(b"null"), None),
                    Err(RpcError::Transport(TransportErrorKind::MissingBatchResponse(_))) => {
                        observation.finish(200, Some(b"[]"), None)
                    }
                    Err(RpcError::Transport(TransportErrorKind::Custom(error))) => {
                        #[cfg(feature = "reqwest")]
                        let timeout = error
                            .downcast_ref::<reqwest::Error>()
                            .is_some_and(reqwest::Error::is_timeout);
                        #[cfg(not(feature = "reqwest"))]
                        let timeout = false;
                        let timeout = timeout
                            || error
                                .downcast_ref::<std::io::Error>()
                                .is_some_and(|e| e.kind() == std::io::ErrorKind::TimedOut);
                        observation.finish(
                            0,
                            None,
                            Some(if timeout { "timeout" } else { "network" }),
                        );
                    }
                    Err(_) => observation.finish(0, None, Some("network")),
                }
            }
            result
        })
    }
}
