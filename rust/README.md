# RPC X-Ray for Rust

Prometheus metrics for reqwest 0.13 and Alloy 2.5 HTTP JSON-RPC clients. Rust 1.94.1 or newer.

## Install

The crate name is `solidrpc-rpc-xray` and the import is `solidrpc_rpc_xray`. This SDK is not published yet. After its first crates.io release:

```bash
cargo add solidrpc-rpc-xray
```

For development, use a path dependency pointing to this directory. The [complete example](../examples/plain-rust/README.md) already uses one. Package builds do not publish anything.

## reqwest

```rust,no_run
use solidrpc_rpc_xray::Xray;

async fn call(client: &reqwest::Client, url: &str) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let telemetry = Xray::default();
    let request = client.post(url).json(&serde_json::json!({
        "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []
    })).build()?;
    let response = telemetry.execute(client, request, "ethereum").await?;
    let body: serde_json::Value = response.json().await?;
    let _ = (body, telemetry.metrics());
    Ok(())
}
```

Keep one telemetry instance per process and expose its `metrics()` at `GET /metrics` using `solidrpc_rpc_xray::CONTENT_TYPE`. Clones share the same registry.

`execute` uses your existing client and request unchanged. It returns an explicit `solidrpc_rpc_xray::Response` wrapper, with `status()`, `headers()`, and `url()` available through dereferencing. Consume it through `bytes()`, `json()`, `chunk()`, or `bytes_stream()`. Transport and body errors remain the original reqwest errors. `json()` returns a boxed transport or serde error. The wrapper does not provide every consuming method on reqwest's Response.

Only buffered POST bodies are observed. Response bytes are copied as consumed up to 1 MiB by default. Oversized or unread responses use HTTP status alone. The wrapper adds no reads, retries, timeout changes, or RPC calls. Cancellation before headers counts as `timeout`. Dropping an unread response uses HTTP status only. This SDK does not install a panic hook or catch application panics.

## Alloy

Enable the `alloy` feature and add the layer to your RPC client, below retry or fallback layers when you want individual attempts measured:

```rust,ignore
use alloy::{providers::ProviderBuilder, rpc::client::ClientBuilder};
use solidrpc_rpc_xray::{Xray, XrayLayer};

let telemetry = Xray::default();
let client = ClientBuilder::default()
    .layer(XrayLayer::new(telemetry.clone(), url.as_str(), "ethereum"))
    .connect_http(url);
let provider = ProviderBuilder::new().connect_client(client);
```

The layer preserves Alloy request and response packets and errors. It classifies decoded JSON-RPC responses, HTTP errors, and transport failures. Serialization for inspection is bounded. It measures the operation at its position in the Tower stack. Enable the default `reqwest` feature to recognize reqwest timeout errors. Subscriptions and WebSocket traffic are outside this HTTP adapter's scope.

## Metrics and optional probes

The [shared contract](../spec/README.md) supplies the same labels, buckets, and seven error outcomes as the other SDKs. Unknown methods use `other`. Provider labels contain only the host. Endpoint and metric-series counts are bounded.

The SDK is passive. Supply a chain label explicitly. It does not detect chains, run head probes, or print offers, and exports `rpc_xray_offers_enabled 0`. `Options` accepts `max_body_bytes`, `value_per_request`, and `revenue_per_hour`.

For application-managed probes, call `telemetry.record_probe(url, chain, Some(head))` or `None` on failure after a separate unwrapped probe. These observations enable head, head age, last advance, and lag gauges and increment only the probe counter. With no observations, head gauges remain absent. Scraping metrics never contacts an RPC endpoint.

## Test and package

From this directory:

```bash
cargo test --all-features
cargo clippy --all-targets --all-features -- -D warnings
cargo package --allow-dirty
```

Tests use shared vectors and local mocks. `cargo package` validates a distributable crate without publishing it.
