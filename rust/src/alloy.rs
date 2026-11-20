use crate::Xray;
use alloy_json_rpc::{RequestPacket, ResponsePacket};
use alloy_transport::{TransportError, TransportErrorKind, TransportFut};
use std::{
    task::{Context, Poll},
    time::Duration,
};
use tower::Service;

#[derive(Clone)]
pub struct XrayHttp {
    client: reqwest::Client,
    url: url::Url,
    xray: Xray,
    chain: String,
}

impl XrayHttp {
    pub fn new(
        client: reqwest::Client,
        url: url::Url,
        xray: Xray,
        chain: impl Into<String>,
    ) -> Self {
        Self {
            client,
            url,
            xray,
            chain: chain.into(),
        }
    }

    pub fn guess_local(&self) -> bool {
        alloy_transport::utils::guess_local_url(&self.url)
    }

    async fn execute(self, packet: RequestPacket) -> Result<ResponsePacket, TransportError> {
        let request = self
            .client
            .post(self.url)
            .json(&packet)
            .headers(packet.headers())
            .build()
            .map_err(TransportErrorKind::custom)?;
        let observation = request
            .body()
            .and_then(|body| body.as_bytes())
            .and_then(|body| self.xray.start(request.url().as_str(), &self.chain, body));
        let mut pending = crate::Pending(observation);
        let response = match self.client.execute(request).await {
            Ok(response) => response,
            Err(error) => {
                if let Some(observation) = pending.0.take() {
                    observation.finish(0, None, Some(failure(&error)));
                }
                return Err(TransportErrorKind::custom(error));
            }
        };
        let status = response.status();
        let retry_after = response
            .headers()
            .get(reqwest::header::RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.trim().parse().ok())
            .map(Duration::from_secs);
        let cap = self.xray.options.max_body_bytes;
        if status.as_u16() >= 400
            || response
                .headers()
                .get(reqwest::header::CONTENT_ENCODING)
                .is_some_and(|value| value != "identity")
            || response
                .content_length()
                .is_some_and(|length| length > cap as u64)
        {
            if let Some(observation) = pending.0.take() {
                observation.finish(status.as_u16(), None, None);
            }
        }
        let body = match response.bytes().await {
            Ok(body) => {
                if let Some(observation) = pending.0.take() {
                    observation.finish(
                        status.as_u16(),
                        (body.len() <= cap).then_some(body.as_ref()),
                        None,
                    );
                }
                body
            }
            Err(error) => {
                if let Some(observation) = pending.0.take() {
                    observation.finish(status.as_u16(), None, Some(failure(&error)));
                }
                if !status.is_success() {
                    return Err(TransportErrorKind::http_error_with_retry_after(
                        status.as_u16(),
                        format!("<failed to read response body: {error}>"),
                        retry_after,
                    ));
                }
                return Err(TransportErrorKind::custom(error));
            }
        };
        if !status.is_success() {
            if let Ok(response) = serde_json::from_slice::<ResponsePacket>(&body) {
                if response.is_error() {
                    return Ok(response);
                }
            }
            return Err(TransportErrorKind::http_error_with_retry_after(
                status.as_u16(),
                String::from_utf8_lossy(&body).into_owned(),
                retry_after,
            ));
        }
        serde_json::from_slice(&body)
            .map_err(|error| TransportError::deser_err(error, String::from_utf8_lossy(&body)))
    }
}

impl Service<RequestPacket> for XrayHttp {
    type Response = ResponsePacket;
    type Error = TransportError;
    type Future = TransportFut<'static>;

    fn poll_ready(&mut self, _: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        Poll::Ready(Ok(()))
    }

    fn call(&mut self, request: RequestPacket) -> Self::Future {
        Box::pin(self.clone().execute(request))
    }
}

fn failure(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() {
        "timeout"
    } else {
        "network"
    }
}
