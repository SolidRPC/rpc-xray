use crate::{Observation, Xray};
use bytes::Bytes;
use futures_util::{Stream, StreamExt};
use reqwest::{Client, Request};
use serde::de::DeserializeOwned;

pub struct Response {
    inner: reqwest::Response,
    observation: Option<Observation>,
    buffer: Vec<u8>,
    cap: usize,
}
impl std::ops::Deref for Response {
    type Target = reqwest::Response;
    fn deref(&self) -> &Self::Target {
        &self.inner
    }
}

impl Xray {
    pub async fn execute(
        &self,
        client: &Client,
        request: Request,
        chain: &str,
    ) -> Result<Response, reqwest::Error> {
        let observation = if request.method() == reqwest::Method::POST {
            request
                .body()
                .and_then(|body| body.as_bytes())
                .and_then(|body| self.start(request.url().as_str(), chain, body))
        } else {
            None
        };
        let mut pending = crate::Pending(observation);
        match client.execute(request).await {
            Ok(inner) => {
                let mut response = Response {
                    inner,
                    observation: pending.0.take(),
                    buffer: Vec::new(),
                    cap: self.options.max_body_bytes,
                };
                if response.status().as_u16() >= 400
                    || response
                        .headers()
                        .get(reqwest::header::CONTENT_ENCODING)
                        .is_some_and(|v| v != "identity")
                    || response
                        .content_length()
                        .is_some_and(|length| length > response.cap as u64)
                {
                    response.finish(None, None);
                }
                Ok(response)
            }
            Err(error) => {
                if let Some(observation) = pending.0.take() {
                    observation.finish(0, None, Some(failure_kind(&error)));
                }
                Err(error)
            }
        }
    }
}

fn failure_kind(error: &reqwest::Error) -> &'static str {
    if error.is_timeout() {
        "timeout"
    } else {
        "network"
    }
}
impl Response {
    fn finish(&mut self, body: Option<&[u8]>, failure: Option<&str>) {
        if let Some(observation) = self.observation.take() {
            observation.finish(self.inner.status().as_u16(), body, failure);
        }
    }
    pub async fn chunk(&mut self) -> Result<Option<Bytes>, reqwest::Error> {
        match self.inner.chunk().await {
            Ok(Some(chunk)) => {
                if self.observation.is_some() {
                    if self.buffer.len() + chunk.len() > self.cap {
                        self.finish(None, None);
                        self.buffer.clear();
                    } else {
                        self.buffer.extend_from_slice(&chunk);
                    }
                }
                Ok(Some(chunk))
            }
            Ok(None) => {
                if let Some(observation) = self.observation.take() {
                    observation.finish(self.inner.status().as_u16(), Some(&self.buffer), None);
                }
                Ok(None)
            }
            Err(error) => {
                self.finish(None, Some(failure_kind(&error)));
                Err(error)
            }
        }
    }
    pub async fn bytes(mut self) -> Result<Bytes, reqwest::Error> {
        let mut bytes = Vec::new();
        while let Some(chunk) = self.chunk().await? {
            bytes.extend_from_slice(&chunk);
        }
        Ok(Bytes::from(bytes))
    }
    pub async fn json<T: DeserializeOwned>(
        self,
    ) -> Result<T, Box<dyn std::error::Error + Send + Sync>> {
        Ok(serde_json::from_slice(&self.bytes().await?)?)
    }
    pub fn bytes_stream(self) -> impl Stream<Item = Result<Bytes, reqwest::Error>> {
        futures_util::stream::unfold(Some(self), |state| async move {
            let mut response = state?;
            match response.chunk().await {
                Ok(Some(chunk)) => Some((Ok(chunk), Some(response))),
                Ok(None) => None,
                Err(error) => Some((Err(error), None)),
            }
        })
        .boxed()
    }
}
impl Drop for Response {
    fn drop(&mut self) {
        self.finish(None, None);
    }
}
