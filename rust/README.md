# RPC X-Ray for Rust

Prometheus metrics for reqwest 0.13 and Alloy 2.5 HTTP JSON-RPC clients. Rust 1.94.1 or newer.

## Install

The crate name is `solidrpc-rpc-xray` and the import is `solidrpc_rpc_xray`. Install a released version from crates.io with Cargo.

```bash
cargo add solidrpc-rpc-xray
```

The [complete reqwest example](https://github.com/SolidRPC/rpc-xray/blob/main/examples/plain-rust/README.md) uses the crate from crates.io and exposes its metrics through an HTTP server.

## reqwest

```rust,no_run
use solidrpc_rpc_xray::Xray;

async fn call(telemetry: &Xray, client: &reqwest::Client, url: &str) -> Result<serde_json::Value, Box<dyn std::error::Error + Send + Sync>> {
    let request = client.post(url).json(&serde_json::json!({
        "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []
    })).build()?;
    let response = telemetry.execute(client, request, "ethereum").await?;
    response.json().await
}
```

Create `let telemetry = Xray::default();` once at application startup. Pass `&telemetry` to every call and expose its `metrics()` at `GET /metrics` using `solidrpc_rpc_xray::CONTENT_TYPE`. Clones share the same registry.

`execute` uses your existing client and request unchanged. It returns an explicit `solidrpc_rpc_xray::Response` wrapper, with `status()`, `headers()`, and `url()` available through dereferencing. Consume it through `bytes()`, `json()`, `chunk()`, or `bytes_stream()`. Transport and body errors remain the original reqwest errors. `json()` returns a boxed transport or serde error. The wrapper does not provide every consuming method on reqwest's Response.

Only buffered POST bodies are observed. Response bytes are copied as consumed up to 1 MiB by default. Oversized or unread responses use HTTP status alone. The wrapper adds no reads, retries, timeout changes, or RPC calls. Cancellation before headers counts as `timeout`. Dropping an unread response uses HTTP status only. This SDK does not install a panic hook or catch application panics.

## Alloy

Enable `alloy` and use `XrayHttp` as the HTTP transport. This feature includes reqwest support, including when default features are disabled. It uses the reqwest client you already configured. Provider calls remain unchanged.

```toml
[dependencies]
alloy = { version = "2.5", default-features = false, features = ["providers", "rpc-client", "reqwest-rustls-tls"] }
solidrpc-rpc-xray = { version = "0.1", features = ["alloy"] }
reqwest = { version = "0.13", default-features = false, features = ["json", "rustls"] }
```

```rust,no_run
use alloy::{providers::ProviderBuilder, rpc::client::ClientBuilder};
use solidrpc_rpc_xray::{Xray, XrayHttp};

fn integrate(existing_http: reqwest::Client, url: reqwest::Url) {
    let telemetry = Xray::default();
    let transport = XrayHttp::new(existing_http, url, telemetry.clone(), "ethereum");
    let is_local = transport.guess_local();
    let client = ClientBuilder::default().transport(transport, is_local);
    let provider = ProviderBuilder::new().connect_client(client);
    let _ = (provider, telemetry);
}
```

Expose the same `telemetry.metrics()` at `/metrics`. The [complete Alloy example](https://github.com/SolidRPC/rpc-xray/blob/main/examples/alloy/README.md) includes the provider, HTTP server, shutdown handling, and matching Prometheus configuration. Its build and mock integration test run in CI.

`XrayHttp` observes HTTP status before Alloy decodes JSON-RPC errors. HTTP 500 is `http_error` and HTTP 429 is `rate_limited`, even when the body is a valid JSON-RPC error or success packet. The packets and errors returned to Alloy callers match its HTTP transport, including error bodies and `Retry-After`. Inspection is bounded by `max_body_bytes`. Like Alloy's HTTP transport, it reads the full response body for decoding, with no additional reads for telemetry. It does not change your client headers, timeout, retry, redirect, or TLS configuration and makes no discovery or probe calls.

Use this transport once per request path. Do not also wrap the same request with `Xray::execute` or another RPC X-Ray adapter. If you add Alloy retry or fallback layers through `ClientBuilder`, the transport measures each attempt that reaches it. Retries or redirects configured inside the existing reqwest client remain one transport operation. Subscriptions and WebSocket traffic are outside this HTTP transport's scope.

The unreleased `XrayLayer` packet adapter has been replaced by `XrayHttp`. Packet-level instrumentation cannot preserve HTTP status precedence because Alloy can discard the status when decoding an error body.

## Metrics and optional probes

The [shared contract](https://github.com/SolidRPC/rpc-xray/blob/main/spec/README.md) supplies the same labels, buckets, and seven error outcomes as the other SDKs. Unknown methods use `other`. Provider labels contain only the host and any non-default port. Endpoint and metric-series counts are bounded.

The SDK adds no RPC calls. Supply a chain label explicitly. It does not detect chains or run head probes. `Options` accepts `max_body_bytes`, `value_per_request`, `revenue_per_hour`, `offers`, and `logger`.

Head freshness comes from traffic you already send. A successful `eth_blockNumber` response or a block object returned by `eth_getBlockByNumber("latest", false)` or its full-transaction variant updates the head, head age, last-advance and lag gauges for that chain and provider. A batch entry counts too. No extra request is made. Head gauges are exported while observations stay fresh, up to 120 seconds after the last one, so an app that stops polling does not raise a false stale-head incident. With one observed provider, lag is zero. A second fresh provider on the same chain makes comparison possible. Alloy's `get_block_number` sends `eth_blockNumber`, so most Alloy apps get head metrics with no change.

If your app never reads the latest head, call `telemetry.record_probe(url, chain, Some(head))`, or `None` on failure, after a separate unwrapped probe. Probe results are also counted in `rpc_xray_probe_requests_total`. With no head observations, head gauges remain absent. Scraping metrics never contacts an RPC endpoint.

## Test and package

From this directory:

```bash
cargo test --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo package --allow-dirty
```

Tests use shared vectors and local mocks. `cargo package` validates a distributable crate without publishing it.
