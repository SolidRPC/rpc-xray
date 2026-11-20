#![cfg(feature = "alloy")]

use alloy::{
    providers::{Provider, ProviderBuilder},
    rpc::client::ClientBuilder,
    transports::http::Http,
};
use alloy_json_rpc::{Id, Request, RequestPacket, ResponsePacket};
use alloy_transport::TransportError;
use solidrpc_rpc_xray::{Options, Xray, XrayHttp};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};
use tower::Service;

const OK: &str = r#"{"jsonrpc":"2.0","id":1,"result":"0x10"}"#;
const RPC_ERROR: &str = r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"header not found","data":{"unchanged":true}}}"#;
const CALLER_ERROR: &str =
    r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"invalid params"}}"#;

#[derive(Clone)]
struct Reply {
    status: u16,
    body: String,
    header_delay: Duration,
    body_delay: Duration,
    truncate: bool,
    disconnect: bool,
}
impl Reply {
    fn new(status: u16, body: impl Into<String>) -> Self {
        Self {
            status,
            body: body.into(),
            header_delay: Duration::ZERO,
            body_delay: Duration::ZERO,
            truncate: false,
            disconnect: false,
        }
    }
}
#[derive(Debug, PartialEq)]
struct Received {
    headers: BTreeMap<String, String>,
    body: Vec<u8>,
}
struct Server {
    url: url::Url,
    received: Arc<Mutex<Vec<Received>>>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn server(reply: Reply) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/private?key=secret",
        listener.local_addr().unwrap()
    )
    .parse()
    .unwrap();
    let received = Arc::new(Mutex::new(Vec::new()));
    let captures = received.clone();
    let task = tokio::spawn(async move {
        loop {
            let (mut stream, _) = listener.accept().await.unwrap();
            let reply = reply.clone();
            let captures = captures.clone();
            tokio::spawn(async move {
                let mut request = Vec::new();
                let mut chunk = [0; 4096];
                loop {
                    let n = stream.read(&mut chunk).await.unwrap();
                    if n == 0 {
                        return;
                    }
                    request.extend_from_slice(&chunk[..n]);
                    if let Some(end) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                        let text = String::from_utf8_lossy(&request[..end]);
                        let headers: BTreeMap<_, _> = text
                            .lines()
                            .skip(1)
                            .filter_map(|line| line.split_once(':'))
                            .map(|(name, value)| (name.to_lowercase(), value.trim().to_owned()))
                            .collect();
                        let length = headers
                            .get("content-length")
                            .unwrap()
                            .parse::<usize>()
                            .unwrap();
                        if request.len() >= end + 4 + length {
                            captures.lock().unwrap().push(Received {
                                headers,
                                body: request[end + 4..end + 4 + length].to_vec(),
                            });
                            break;
                        }
                    }
                }
                if reply.disconnect {
                    return;
                }
                tokio::time::sleep(reply.header_delay).await;
                let length = reply.body.len() + usize::from(reply.truncate);
                let headers = format!("HTTP/1.1 {} Fixture\r\nContent-Type: application/json\r\nContent-Length: {length}\r\nRetry-After: 7\r\nConnection: close\r\n\r\n",reply.status);
                if stream.write_all(headers.as_bytes()).await.is_err() {
                    return;
                }
                tokio::time::sleep(reply.body_delay).await;
                let _ = stream.write_all(reply.body.as_bytes()).await;
            });
        }
    });
    Server {
        url,
        received,
        task,
    }
}
fn packet() -> RequestPacket {
    let mut request = Request::new("eth_blockNumber", Id::Number(1), ());
    request
        .meta
        .headers_mut()
        .insert("x-request", "packet-value".parse().unwrap());
    request.serialize().unwrap().into()
}
fn http_client(timeout: Duration) -> reqwest::Client {
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert("x-client", "existing-client-value".parse().unwrap());
    reqwest::Client::builder()
        .default_headers(headers)
        .timeout(timeout)
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .build()
        .unwrap()
}
async fn compare(
    reply: Reply,
    packet: RequestPacket,
    options: Options,
) -> (Xray, Result<ResponsePacket, TransportError>) {
    let server = server(reply).await;
    let http = http_client(Duration::from_millis(100));
    let mut stock = Http::with_client(http.clone(), server.url.clone());
    let xray = Xray::new(options);
    let mut wrapped = XrayHttp::new(http, server.url.clone(), xray.clone(), "ethereum");
    let expected = stock.call(packet.clone()).await;
    let actual = wrapped.call(packet).await;
    assert_eq!(format!("{actual:?}"), format!("{expected:?}"));
    {
        let captures = server.received.lock().unwrap();
        assert_eq!(captures.len(), 2);
        assert_eq!(captures[0], captures[1]);
        assert_eq!(captures[0].headers["x-client"], "existing-client-value");
    }
    for _ in 0..5 {
        assert!(!xray.metrics().contains("secret"));
    }
    tokio::time::sleep(Duration::from_millis(10)).await;
    assert_eq!(server.received.lock().unwrap().len(), 2);
    (xray, actual)
}
fn assert_outcome(xray: &Xray, outcome: &str) {
    let metrics = xray.metrics();
    assert!(
        metrics.contains(&format!(
            "method=\"eth_blockNumber\",outcome=\"{outcome}\"}} 1"
        )),
        "{metrics}"
    );
    assert_eq!(
        metrics
            .lines()
            .filter(|line| line.starts_with("rpc_xray_requests_total{"))
            .count(),
        1
    );
    assert!(metrics.contains("method=\"eth_blockNumber\"} 1"));
}

#[tokio::test]
async fn real_http_transport_preserves_packets_errors_headers_and_status_priority() {
    for (status, body, outcome) in [
        (200, OK, "ok"),
        (200, RPC_ERROR, "rpc_error"),
        (200, CALLER_ERROR, "caller_error"),
        (
            200,
            r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32005,"message":"rate limit"}}"#,
            "rate_limited",
        ),
        (200, "broken JSON", "rpc_error"),
        (500, "unavailable", "http_error"),
        (429, "too many requests", "rate_limited"),
        (500, RPC_ERROR, "http_error"),
        (429, CALLER_ERROR, "rate_limited"),
        (500, OK, "http_error"),
        (429, OK, "rate_limited"),
    ] {
        let (xray, _) = compare(Reply::new(status, body), packet(), Options::default()).await;
        assert_outcome(&xray, outcome);
    }
}

#[tokio::test]
async fn existing_timeouts_and_body_errors_keep_original_alloy_semantics() {
    let mut headers_timeout = Reply::new(200, OK);
    headers_timeout.header_delay = Duration::from_millis(200);
    let mut body_timeout = Reply::new(200, OK);
    body_timeout.body_delay = Duration::from_millis(200);
    let mut disconnected = Reply::new(200, OK);
    disconnected.disconnect = true;
    let mut truncated = Reply::new(200, OK);
    truncated.truncate = true;
    let mut error_truncated = Reply::new(503, "unavailable");
    error_truncated.truncate = true;
    for (reply, outcome) in [
        (headers_timeout, "timeout"),
        (body_timeout, "timeout"),
        (disconnected, "network"),
        (truncated, "network"),
        (error_truncated, "http_error"),
    ] {
        let (xray, _) = compare(reply, packet(), Options::default()).await;
        assert_outcome(&xray, outcome);
    }
}

#[tokio::test]
async fn batches_preserve_response_order_and_missing_members() {
    let first = Request::new("eth_blockNumber", Id::Number(1), ())
        .serialize()
        .unwrap();
    let second = Request::new("eth_chainId", Id::Number(2), ())
        .serialize()
        .unwrap();
    let request = RequestPacket::Batch(vec![first, second]);
    for (body, expected) in [
        (
            format!("[{CALLER_ERROR},{}]", OK.replace("\"id\":1", "\"id\":2")),
            vec![("eth_blockNumber", "caller_error"), ("eth_chainId", "ok")],
        ),
        (
            format!("[{},{}]", CALLER_ERROR.replace("\"id\":1", "\"id\":2"), OK),
            vec![("eth_blockNumber", "ok"), ("eth_chainId", "caller_error")],
        ),
        (
            format!("[{OK}]"),
            vec![("eth_blockNumber", "ok"), ("eth_chainId", "rpc_error")],
        ),
    ] {
        let (xray, _) = compare(Reply::new(200, body), request.clone(), Options::default()).await;
        let metrics = xray.metrics();
        for (method, outcome) in expected {
            assert!(
                metrics.contains(&format!("method=\"{method}\",outcome=\"{outcome}\"}} 1")),
                "{metrics}"
            );
        }
    }
    let (xray, _) = compare(
        Reply::new(429, format!("[{OK},{CALLER_ERROR}]")),
        request,
        Options::default(),
    )
    .await;
    assert_eq!(
        xray.metrics()
            .matches("outcome=\"rate_limited\"} 1")
            .count(),
        2
    );
}

#[tokio::test]
async fn inspection_caps_preserve_full_response_and_request() {
    let long = format!(
        r#"{{"jsonrpc":"2.0","id":1,"error":{{"code":-32000,"message":"{}"}}}}"#,
        "x".repeat(2048)
    );
    let (xray, result) = compare(
        Reply::new(200, long),
        packet(),
        Options {
            max_body_bytes: 256,
            ..Options::default()
        },
    )
    .await;
    assert!(result.unwrap().is_error());
    assert_outcome(&xray, "ok");
    let large = Request::new("eth_call", Id::Number(1), vec!["x".repeat(2048)])
        .serialize()
        .unwrap()
        .into();
    let (xray, _) = compare(
        Reply::new(200, OK),
        large,
        Options {
            max_body_bytes: 256,
            ..Options::default()
        },
    )
    .await;
    assert!(!xray.metrics().contains("rpc_xray_requests_total{"));
}

#[tokio::test]
async fn real_provider_builder_compiles_and_records_exactly_one_call() {
    let server = server(Reply::new(200, OK.replace("\"id\":1", "\"id\":0"))).await;
    let xray = Xray::default();
    let transport = XrayHttp::new(
        http_client(Duration::from_secs(1)),
        server.url.clone(),
        xray.clone(),
        "ethereum",
    );
    let is_local = transport.guess_local();
    let client = ClientBuilder::default().transport(transport, is_local);
    let provider = ProviderBuilder::new().connect_client(client);
    assert_eq!(provider.get_block_number().await.unwrap(), 16);
    assert_eq!(server.received.lock().unwrap().len(), 1);
    assert_outcome(&xray, "ok");
}

#[tokio::test]
async fn cancellation_before_headers_and_during_body_records_once() {
    for during_body in [false, true] {
        let mut reply = Reply::new(200, OK);
        if during_body {
            reply.body_delay = Duration::from_millis(200);
        } else {
            reply.header_delay = Duration::from_millis(200);
        }
        let server = server(reply).await;
        let xray = Xray::default();
        let mut transport = XrayHttp::new(
            http_client(Duration::from_secs(1)),
            server.url.clone(),
            xray.clone(),
            "ethereum",
        );
        assert!(
            tokio::time::timeout(Duration::from_millis(50), transport.call(packet()))
                .await
                .is_err()
        );
        assert_eq!(server.received.lock().unwrap().len(), 1);
        assert_outcome(&xray, "timeout");
    }
}

#[tokio::test]
async fn null_id_packets_preserve_alloys_wire_representation() {
    let request = Request::new("eth_chainId", Id::None, ())
        .serialize()
        .unwrap();
    let body = r#"{"jsonrpc":"2.0","id":null,"result":"0x1"}"#;
    let (xray, _) = compare(
        Reply::new(200, body),
        request.clone().into(),
        Options::default(),
    )
    .await;
    assert!(xray
        .metrics()
        .contains("method=\"eth_chainId\",outcome=\"ok\"} 1"));
    let first = Request::new("eth_blockNumber", Id::Number(1), ())
        .serialize()
        .unwrap();
    let (xray, _) = compare(
        Reply::new(200, format!("[{OK},{body}]")),
        RequestPacket::Batch(vec![first, request]),
        Options::default(),
    )
    .await;
    assert_eq!(xray.metrics().matches("outcome=\"ok\"} 1").count(), 2);
}
