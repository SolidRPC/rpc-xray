#![cfg(feature = "alloy")]

use alloy_json_rpc::{Id, Request, RequestPacket, ResponsePacket};
use alloy_transport::{TransportError, TransportErrorKind};
use solidrpc_rpc_xray::{Xray, XrayLayer};
use std::{
    future::{ready, Ready},
    task::{Context, Poll},
};
use tower::{Layer, Service};

struct Mock {
    result: Option<Result<ResponsePacket, TransportError>>,
}
impl Service<RequestPacket> for Mock {
    type Response = ResponsePacket;
    type Error = TransportError;
    type Future = Ready<Result<ResponsePacket, TransportError>>;
    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }
    fn call(&mut self, packet: RequestPacket) -> Self::Future {
        assert_eq!(packet.requests()[0].method(), "eth_call");
        ready(self.result.take().unwrap())
    }
}

#[tokio::test]
async fn layer_preserves_responses_and_http_errors() {
    for (result, outcome) in [
        (
            Ok(serde_json::from_str(r#"{"jsonrpc":"2.0","id":1,"result":null}"#).unwrap()),
            "ok",
        ),
        (
            Ok(serde_json::from_str(
                r#"{"jsonrpc":"2.0","id":1,"error":{"code":3,"message":"revert"}}"#,
            )
            .unwrap()),
            "caller_error",
        ),
        (
            Err(TransportErrorKind::http_error(429, "fixture".into())),
            "rate_limited",
        ),
        (
            Err(TransportErrorKind::http_error(503, "fixture".into())),
            "http_error",
        ),
    ] {
        let xray = Xray::default();
        let expected = format!("{result:?}");
        let mut service = XrayLayer::new(
            xray.clone(),
            "https://rpc.example/private?key=secret",
            "ethereum",
        )
        .layer(Mock {
            result: Some(result),
        });
        let request = Request::new("eth_call", Id::Number(1), ())
            .serialize()
            .unwrap()
            .into();
        let actual = service.call(request).await;
        assert_eq!(format!("{actual:?}"), expected);
        let metrics = xray.metrics();
        assert!(
            metrics.contains(&format!("outcome=\"{outcome}\"}} 1")),
            "{metrics}"
        );
        assert!(!metrics.contains("secret"));
    }
}
