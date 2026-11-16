# RPC X-Ray for Go

A standard-library HTTP transport wrapper with Prometheus metrics. Go 1.23 or newer. No runtime dependencies outside the standard library.

## Install

The module path is `github.com/SolidRPC/rpc-xray/go`. After the first Go release tag:

```bash
go get github.com/SolidRPC/rpc-xray/go@v0.1.0
```

This SDK is not released yet. For development, add a local `replace` directive pointing to this directory, as the [complete example](../examples/plain-go/go.mod) does. Releases from this subdirectory require tags such as `go/v0.1.0`.

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

The wrapper only inspects POST bodies that provide `Request.GetBody`. `http.NewRequest` supplies this for `bytes.Reader`, `bytes.Buffer`, and `strings.Reader`. Non-replayable streaming requests are skipped without consuming them. Responses retain their status, headers, and body bytes. The body wrapper copies at most 1 MiB as the caller reads, with no read-ahead or extra network calls. Always close response bodies.

Complete inspected bodies get JSON-RPC classification. Oversized, still-encoded, or prematurely closed bodies use HTTP status only. Read errors preserve the original error and record `network` or `timeout`. Context cancellation counts as `timeout`. The wrapper neither retries nor sets deadlines.

## Metrics and optional probes

All metric names, labels, buckets, and outcomes follow the [shared specification](../spec/README.md). Unknown methods use `other`. Provider labels contain only host and non-default port. Paths, query strings, credentials, and upstream messages are never exported. A mutex protects concurrent requests and scrapes. Endpoint and series counts are bounded.

The SDK is passive. It does not discover chain IDs, schedule probes, or print offers. Supply `Chain`, otherwise it exports `unknown`. `rpc_xray_offers_enabled` is `0`. Set `ValuePerRequest` or `RevenuePerHour` pointers in `Options` to export the optional USD gauges.

If your app runs a separate head probe through an unwrapped client, call `telemetry.RecordProbe(url, chain, &head)` or `nil` on failure. Probe observations populate the four head gauges and the separate probe counter. With no probes, head gauges are absent. Metrics scrapes do not send RPC requests.

For custom clients, `telemetry.Start(url, chain, requestBytes)` returns an optional observation with `Finish(status, responseBytes, failure)`. Supply an empty failure for HTTP responses or `timeout` / `network`. `Finish` is nil-safe and records at most once.

## Test

From this directory:

```bash
go test -race ./...
go vet ./...
```

The [complete server example](../examples/plain-go/README.md) includes polling, `/metrics`, shutdown, and a matching Prometheus configuration.
