#![cfg(feature = "reqwest")]

use solidrpc_rpc_xray::{Options, Xray};
use std::{
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
};

const REQUEST: &str = r#"{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}"#;
const OK: &str = r#"{"jsonrpc":"2.0","id":1,"result":"0x10"}"#;

struct Server {
    url: String,
    calls: Arc<AtomicUsize>,
    task: tokio::task::JoinHandle<()>,
}
impl Drop for Server {
    fn drop(&mut self) {
        self.task.abort();
    }
}
async fn server(status: u16, body: String, delay: Duration) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!(
        "http://{}/private?key=fixture-secret",
        listener.local_addr().unwrap()
    );
    let calls = Arc::new(AtomicUsize::new(0));
    let counter = calls.clone();
    let task = tokio::spawn(async move {
        loop {
            let (mut stream, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            let mut chunk = [0; 4096];
            loop {
                let n = stream.read(&mut chunk).await.unwrap();
                if n == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..n]);
                if let Some(header_end) = request.windows(4).position(|w| w == b"\r\n\r\n") {
                    let headers = String::from_utf8_lossy(&request[..header_end]).to_lowercase();
                    let length: usize = headers
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length: "))
                        .unwrap_or("0")
                        .parse()
                        .unwrap();
                    if request.len() >= header_end + 4 + length {
                        break;
                    }
                }
            }
            counter.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(delay).await;
            let response=format!("HTTP/1.1 {status} Test\r\nContent-Length: {}\r\nContent-Type: application/json\r\nX-Fixture: unchanged\r\nConnection: close\r\n\r\n{body}",body.len());
            let _ = stream.write_all(response.as_bytes()).await;
        }
    });
    Server { url, calls, task }
}

#[tokio::test]
async fn passthrough_classification_and_no_extra_calls() {
    for (status, body, outcome) in [
        (200, OK, "ok"),
        (429, "rate limited", "rate_limited"),
        (503, "unavailable", "http_error"),
        (
            200,
            r#"{"id":1,"error":{"code":3,"message":"revert"}}"#,
            "caller_error",
        ),
        (200, "broken JSON", "rpc_error"),
    ] {
        let server = server(status, body.into(), Duration::ZERO).await;
        let xray = Xray::default();
        let client = reqwest::Client::new();
        let request = client.post(&server.url).body(REQUEST).build().unwrap();
        let response = xray.execute(&client, request, "ethereum").await.unwrap();
        assert_eq!(response.status().as_u16(), status);
        assert_eq!(response.headers()["x-fixture"], "unchanged");
        assert_eq!(response.url().as_str(), server.url);
        assert_eq!(response.bytes().await.unwrap().as_ref(), body.as_bytes());
        for _ in 0..5 {
            let metrics = xray.metrics();
            assert!(
                metrics.contains(&format!("outcome=\"{outcome}\"}} 1")),
                "{metrics}"
            );
            assert!(!metrics.contains("fixture-secret"));
            assert!(!metrics.contains("private"));
        }
        assert_eq!(server.calls.load(Ordering::SeqCst), 1);
    }
}

#[tokio::test]
async fn bounded_capture_unread_and_non_rpc() {
    let server = server(200, "x".repeat(200), Duration::ZERO).await;
    let xray = Xray::new(Options {
        max_body_bytes: 100,
        ..Options::default()
    });
    let client = reqwest::Client::new();
    let response = xray
        .execute(
            &client,
            client.post(&server.url).body(REQUEST).build().unwrap(),
            "base",
        )
        .await
        .unwrap();
    assert_eq!(response.bytes().await.unwrap().len(), 200);
    assert!(xray.metrics().contains("outcome=\"ok\"} 1"));
    let xray = Xray::default();
    let response = xray
        .execute(
            &client,
            client.post(&server.url).body(REQUEST).build().unwrap(),
            "base",
        )
        .await
        .unwrap();
    drop(response);
    assert!(xray.metrics().contains("outcome=\"ok\"} 1"));
    let response = xray
        .execute(&client, client.get(&server.url).build().unwrap(), "base")
        .await
        .unwrap();
    let _ = response.bytes().await.unwrap();
    assert!(xray.metrics().contains("outcome=\"ok\"} 1"));
}

#[tokio::test]
async fn timeout_and_cancellation() {
    let server = server(200, OK.into(), Duration::from_secs(1)).await;
    let xray = Xray::default();
    let client = reqwest::Client::builder()
        .timeout(Duration::from_millis(30))
        .build()
        .unwrap();
    let error = xray
        .execute(
            &client,
            client.post(&server.url).body(REQUEST).build().unwrap(),
            "base",
        )
        .await
        .err()
        .unwrap();
    assert!(error.is_timeout());
    assert!(xray.metrics().contains("outcome=\"timeout\"} 1"));
    let client = reqwest::Client::new();
    let request = client.post(&server.url).body(REQUEST).build().unwrap();
    assert!(tokio::time::timeout(
        Duration::from_millis(30),
        xray.execute(&client, request, "base")
    )
    .await
    .is_err());
    assert!(xray.metrics().contains("outcome=\"timeout\"} 2"));
}

#[test]
fn probes_metrics_bounds_and_shared_clones() {
    let xray = Xray::new(Options {
        value_per_request: Some(0.1),
        revenue_per_hour: Some(100.0),
        ..Options::default()
    });
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let xray = xray.clone();
            std::thread::spawn(move || {
                for _ in 0..50 {
                    xray.start("https://rpc.example", "ethereum", REQUEST.as_bytes())
                        .unwrap()
                        .finish(200, Some(OK.as_bytes()), None);
                    let _ = xray.metrics();
                }
            })
        })
        .collect();
    for thread in threads {
        thread.join().unwrap();
    }
    assert!(xray.metrics().contains("outcome=\"ok\"} 200"));
    xray.record_probe("https://rpc.example", "ethereum", Some(10));
    xray.record_probe("https://second.example", "ethereum", Some(12));
    xray.record_probe("https://rpc.example", "ethereum", None);
    assert!(xray
        .metrics()
        .contains("rpc_xray_head_lag_blocks{chain=\"ethereum\",provider=\"rpc.example\"} 2"));
    for i in 0..40 {
        xray.start(
            &format!("https://rpc{i}.example"),
            "ethereum",
            REQUEST.as_bytes(),
        )
        .unwrap()
        .finish(200, Some(OK.as_bytes()), None);
    }
    assert!(xray.metrics().contains("provider=\"other\""));
    assert_eq!(solidrpc_rpc_xray::chain_label(1), "ethereum");
    assert_eq!(solidrpc_rpc_xray::chain_label(123456), "eip155:123456");
}
