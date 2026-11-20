# RPC X-Ray for Go

A standard-library HTTP transport wrapper with Prometheus metrics. Go 1.23 or newer. No runtime dependencies outside the standard library.

## Install

The module path is `github.com/SolidRPC/rpc-xray/go`. Install the tagged module with the Go CLI.

```bash
go get github.com/SolidRPC/rpc-xray/go@v0.1.0
```

For development, add a local `replace` directive pointing to this directory, as the [complete example](https://github.com/SolidRPC/rpc-xray/blob/main/examples/plain-go/go.mod) does. Releases from this subdirectory require tags such as `go/v0.1.0`.

## Integrate

```go
telemetry := rpcxray.New(rpcxray.Options{})
client := &http.Client{
    Transport: &rpcxray.Transport{
        Xray: telemetry,
        Chain: "ethereum",
        Base: http.DefaultTransport,
    },
    Timeout: 5 * time.Second,
}
mux.Handle("GET /metrics", telemetry.Handler())
```

Import `rpcxray "github.com/SolidRPC/rpc-xray/go"`, `net/http`, and `time`. Wrap your existing transport and preserve your client settings. For `go-ethereum`, pass this HTTP client to `rpc.DialOptions(ctx, url, rpc.WithHTTPClient(client))`, then construct `ethclient.NewClient(rpcClient)`. JSON-RPC calls made by that client flow through the wrapper.

The [complete go-ethereum example](https://github.com/SolidRPC/rpc-xray/blob/main/examples/go-ethereum/README.md) includes a runnable `ethclient` app, `/metrics`, graceful shutdown, local integration tests, and matching Prometheus configuration. It uses a separate module, so the SDK itself keeps its standard-library-only dependency footprint.

The wrapper only inspects POST bodies that provide `Request.GetBody`. `http.NewRequest` supplies this for `bytes.Reader`, `bytes.Buffer`, and `strings.Reader`. Non-replayable streaming requests are skipped without consuming them. Responses retain their status, headers, and body bytes. The body wrapper copies at most 1 MiB as the caller reads, with no read-ahead or extra network calls. Always close response bodies.

Complete inspected bodies get JSON-RPC classification. Oversized, still-encoded, or prematurely closed bodies use HTTP status only. Read errors preserve the original error and record `network` or `timeout`. Context cancellation and `http.Client.Timeout` count as `timeout`, including cancellation while consuming the response body. A configured deadline alone does not turn an unrelated network failure into a timeout. The wrapper neither retries nor sets deadlines.

`rpc_xray_requests_total` counts logical JSON-RPC items. One batch HTTP exchange containing three calls increments this counter three times, with each item's method and outcome. The duration histogram records the exchange once with `method="batch"`. go-ethereum reports item failures on each `rpc.BatchElem.Error`, so a nil error from `BatchCallContext` does not mean every item succeeded. Instrumentation preserves this behavior.

## Metrics and optional probes

All metric names, labels, buckets, and outcomes follow the [shared specification](https://github.com/SolidRPC/rpc-xray/blob/main/spec/README.md). Unknown methods use `other`. Provider labels contain only host and non-default port. Paths, query strings, credentials, and upstream messages are never exported. A mutex protects concurrent requests and scrapes. Endpoint and series counts are bounded.

The SDK adds no RPC calls. It does not discover chain IDs or schedule probes. Supply `Chain`, otherwise it exports `unknown`. Set `ValuePerRequest` or `RevenuePerHour` pointers in `Options` to export the optional USD gauges.

Head freshness comes from traffic you already send. When a wrapped request includes `eth_blockNumber` and the answer is a successful hex block number, that block updates the head, head age, last-advance and lag gauges for that chain and provider. A batch entry counts too. No extra request is made. Head gauges are exported while observations stay fresh, up to 120 seconds after the last one, so an app that stops polling does not raise a false stale-head incident. With one observed provider, lag is zero. A second fresh provider on the same chain makes comparison possible. `ethclient.BlockNumber` sends `eth_blockNumber`, so most go-ethereum apps get head metrics with no change.

If your app never calls `eth_blockNumber`, run a separate head probe through an unwrapped client and call `telemetry.RecordProbe(url, chain, &head)`, or `nil` on failure. Probe results are also counted in `rpc_xray_probe_requests_total`. With no head observations, head gauges are absent. Metrics scrapes do not send RPC requests.

## The SolidRPC offer

`rpc_xray_offers_enabled` is `1` by default. When this process has run for a day, projects about 100M requests a month, and saw at least 1% failed requests or a stale head, it writes one line through the standard `log` package naming the 14-day free trial or $100 credit from SolidRPC and hello@solidrpc.io. It is written once per process. The dashboard and Slack offer use the same gauge. Set `Options.Logger` to send the line elsewhere, or `Options.DisableOffers` to turn the offer off everywhere. Nothing is sent to SolidRPC.

For custom clients, `telemetry.Start(url, chain, requestBytes)` returns an optional observation with `Finish(status, responseBytes, failure)`. Supply an empty failure for HTTP responses or `timeout` / `network`. `Finish` is nil-safe and records at most once.

## Test

From this directory:

```bash
go test -race ./...
go vet ./...
```

The [complete server example](https://github.com/SolidRPC/rpc-xray/blob/main/examples/plain-go/README.md) includes polling, `/metrics`, shutdown, and a matching Prometheus configuration.
