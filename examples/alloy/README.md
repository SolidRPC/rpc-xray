# Alloy with Prometheus

A complete Alloy 2.5 app that reads `eth_blockNumber` from one endpoint at most once per second, serves its latest result at `/`, and exports `/metrics`. It uses the local Rust package while that package awaits its first release. See the [SDK installation guide](../../rust/README.md) for its intended package install command.

It builds the normal Alloy provider around `XrayHttp`, using the app's existing reqwest client. The transport records HTTP status before Alloy decodes the body, so HTTP 429 remains `rate_limited` even when the JSON-RPC body contains an invalid-params error. It preserves Alloy's returned packets and errors.

## Install and run

From this example directory:

```bash
cargo build --locked
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your existing RPC endpoint and its chain label. Quote values containing shell special characters. Keep this ignored file local. Run from this directory:

```bash
set -a
. ./.env
set +a
./target/debug/rpc-xray-alloy-example
```

The app listens on `127.0.0.1:9464` by default. It makes no chain discovery or head probe calls. Polls do not overlap and requests time out after 4.5 seconds. The example disables reqwest retries and redirects. There are no application retries. Metrics scrapes add zero RPC requests. Head gauges remain absent because separate probing is disabled.

```bash
curl http://127.0.0.1:9464/
curl http://127.0.0.1:9464/metrics
```

Expect JSON with `status`, `chain`, and `blockNumber` at `/`. After a successful poll, `/metrics` includes `rpc_xray_requests_total` with `outcome="ok"`. Upstream errors produce `status="error"` without printing upstream messages or endpoint credentials.

Press Ctrl+C to stop. Shutdown stops polling and closes the HTTP listener. Shutdown cancels an in-flight request. Do not leave the example running when you no longer need its RPC traffic.

## Prometheus

In another terminal in this directory:

```bash
docker compose up -d
```

The matching [Prometheus config](prometheus.yml) scrapes `host.docker.internal:9464/metrics` every 15 seconds and loads the shared recording and alert rules. Docker Desktop on macOS and Windows can reach the host through this address. Open [Prometheus targets](http://localhost:9090/targets) and confirm `rpc-xray-alloy` is **UP**. Allow two scrapes, then query:

```promql
sum by (chain, provider) (rate(rpc_xray_requests_total[1m]))
```

A healthy endpoint approaches one request per second. The shared `rpc_xray:success_ratio:rate1h` recording rule becomes available after a rule evaluation. Missing data is not a healthy result.

On native Linux, set `HOST=0.0.0.0` or bind to an interface Docker can reach. Restrict access with your firewall because the example has no HTTP authentication. A native Prometheus process on the same host can use `127.0.0.1:9464`, with an absolute local path to `alerts/rpc-xray.rules.yml`.

If you change `PORT`, update `prometheus.yml`. All language examples use port 9464 by default, so run one at a time or assign different ports. Set `PROMETHEUS_PORT=9091` if 9090 is occupied. The full [Grafana stack](../../stack) can scrape the same endpoint instead of this example's Prometheus.

```bash
docker compose down
```

Stopping Prometheus does not stop the app. Stop the app separately with Ctrl+C.

## Local verification

The repository CI starts this example against a local mock RPC server, checks `/metrics` and request pacing, and confirms shutdown stops traffic. It never uses real provider credentials.
