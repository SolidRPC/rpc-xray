# go-ethereum with Prometheus

A complete `ethclient` app that reads `eth_blockNumber` from one existing endpoint at most once per second, serves its latest result at `/`, and exports `/metrics`. RPC X-Ray wraps the HTTP transport supplied to `rpc.DialOptions`. The application keeps using the usual `ethclient.BlockNumber` API.

This example requires Go 1.24 or newer because it uses go-ethereum v1.17.5. The SDK itself requires Go 1.23 and has no dependencies outside the standard library. This example is a separate module using the released Go SDK. [Installation guide](../../go/README.md)

The example currently installs SDK v0.1.0. If a client decodes one JSON value and closes a chunked HTTP 200 response before EOF, this version can count a provider error as `ok`. [Go transport behavior](../../go/README.md)

## Install and run

From this example directory:

```bash
go mod download
mkdir -p ../../tmp
go build -o ../../tmp/rpc-xray-go-ethereum .
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your existing endpoint and chain label. Quote values containing shell special characters and keep this ignored file local. Start the app from this directory:

```bash
set -a
. ./.env
set +a
../../tmp/rpc-xray-go-ethereum
```

The app listens on `127.0.0.1:9464`. It sends only `eth_blockNumber`, with no chain discovery, head probes, application retries, or overlapping polls. Each call has a 4.5 second deadline. `/metrics` uses the same registry as the transport and adds no RPC requests. Head gauges come from the app's own `eth_blockNumber` answers, so stale-head and lag incidents work with no extra calls.

```bash
curl http://127.0.0.1:9464/
curl http://127.0.0.1:9464/metrics
```

The root endpoint returns `chain`, `status`, and `blockNumber`. A successful poll produces `status="ok"` and `rpc_xray_requests_total` with `method="eth_blockNumber"` and `outcome="ok"`. A failed poll changes the status to `error`, without printing the endpoint URL or upstream message.

Press Ctrl+C to stop. Shutdown cancels an in-flight request, stops polling, closes the metrics listener, and closes the RPC client. Stop the app when you no longer need its provider traffic.

## Integrate an existing client

The essential wiring in [main.go](main.go) is:

```go
telemetry := rpcxray.New(rpcxray.Options{})
httpClient := &http.Client{
    Transport: &rpcxray.Transport{
        Xray: telemetry,
        Chain: "ethereum",
        Base: http.DefaultTransport,
    },
    Timeout: 4500 * time.Millisecond,
}
rpcClient, err := rpc.DialOptions(ctx, rpcURL, rpc.WithHTTPClient(httpClient))
if err != nil {
    return err
}
client := ethclient.NewClient(rpcClient)
defer client.Close()
mux.Handle("GET /metrics", telemetry.Handler())
head, err := client.BlockNumber(ctx)
```

For an existing application, wrap its current transport and retain its headers, timeouts, and client options. The complete file includes imports, environment validation, server setup, polling, and signal handling. This adapter covers HTTP JSON-RPC, not WebSocket subscriptions.

If your app uses `rpcClient.BatchCallContext`, request counters record each logical batch item with its own outcome. One HTTP batch of three calls produces three request observations and one duration observation with `method="batch"`. Inspect each `rpc.BatchElem.Error`, since go-ethereum can return a nil batch-level error when individual items failed.

## Prometheus

In another terminal in this directory:

```bash
docker compose up -d
```

The matching [Prometheus config](prometheus.yml) scrapes `host.docker.internal:9464/metrics` every 15 seconds and loads the shared recording and alert rules. Docker Desktop on macOS and Windows reaches the host through this address. Open [Prometheus targets](http://localhost:9090/targets) and confirm `rpc-xray-go-ethereum` is **UP**. After two scrapes, query:

```promql
sum by (chain, provider) (rate(rpc_xray_requests_total[1m]))
```

A healthy endpoint approaches one request per second. The shared `rpc_xray:success_ratio:rate1h` rule becomes available after evaluation. Missing samples are not evidence of health.

On native Linux, set `HOST=0.0.0.0` or choose an interface Docker can reach. Restrict access with your firewall because the example has no HTTP authentication. A native Prometheus process on the same host can instead target `127.0.0.1:9464` and use an absolute local path to `alerts/rpc-xray.rules.yml`.

If you change `PORT`, update the Prometheus target. Other examples share port 9464, so run one at a time or assign separate ports. Set `PROMETHEUS_PORT=9091` if 9090 is occupied.

```bash
docker compose down
```

This stops Prometheus. Stop the app separately with Ctrl+C.

## Local verification

From this example directory:

```bash
go test -race ./...
go vet ./...
```

Tests use local HTTP fixtures and exercise real go-ethereum clients. They check preserved results and typed batch errors, per-item metrics, header preservation, credentials staying out of labels, no extra requests from metrics scrapes, `http.Client.Timeout`, and shutdown cancellation.

From the repository root, the shared example check builds on the binary created above:

```bash
python3 scripts/test-example.py go-ethereum
```

It verifies `/metrics`, recovery after an RPC error, request pacing, and SIGTERM shutdown without real provider credentials.
