# rpc-xray specification

Every language package implements this contract and passes `vectors.json`. Alert rules and dashboards depend only on what is written here.

## Pass-through rules

1. The wrapped client returns exactly what the underlying client returns: same value, same error, same timing of delivery.
2. Any failure inside rpc-xray is swallowed. It never reaches the caller.
3. Recording happens after the response is handed back. Nothing is awaited on the request path.
4. The URL, method, headers and body of a request are never changed.
5. A response body is inspected only on a copy and only up to `maxBodyBytes` (default 1 MiB). A larger body is classified by HTTP status alone.
6. Only JSON-RPC requests are recorded: a POST whose body is a JSON object or array carrying `method`.
7. No network calls are made except to the endpoints the application already calls.
8. Credentials never appear in metrics or logs. The `provider` label is the URL host.

## Outcomes

One outcome per JSON-RPC call. A batch yields one outcome per entry.

| Outcome | Meaning |
|---|---|
| `ok` | HTTP 2xx or 3xx and no JSON-RPC error |
| `http_error` | HTTP 4xx or 5xx, except 429 |
| `rate_limited` | HTTP 429, or a JSON-RPC error that says the caller was throttled |
| `rpc_error` | HTTP 2xx carrying a JSON-RPC error that is the provider's fault, or a body that is not valid JSON-RPC |
| `caller_error` | HTTP 2xx carrying a JSON-RPC error caused by the request itself: a revert, invalid parameters, an unknown method, a rejected transaction |
| `timeout` | The request was aborted or timed out before a response arrived |
| `network` | The request failed before a response arrived for any other reason |

Classification order for a JSON-RPC error object `{code, message}`:

1. `rate_limited` if `code` is 429, or the lower-cased message matches `rate limit`, `too many requests`, `throttl`, `capacity exceeded`, `quota exceeded` or `exceeded the quota`.
2. `caller_error` if `code` is one of 3, -32600, -32601, -32602, -32700, or the lower-cased message contains one of: `execution reverted`, `revert`, `nonce too low`, `nonce too high`, `already known`, `insufficient funds`, `underpriced`, `intrinsic gas too low`, `gas required exceeds`, `out of gas`, `exceeds block gas limit`, `max fee per gas less than block base fee`, `invalid opcode`, `invalid sender`, `invalid params`, `method not found`, `does not exist/is not available`.
3. `rpc_error` otherwise.

Batches: each request entry takes the outcome of the response entry with the same `id`. An entry with no matching response is `rpc_error`. If the response to a batch is a single object, every entry takes its outcome.

## Labels

| Label | Values |
|---|---|
| `chain` | A name from the built-in chain list, `eip155:<id>` for other chains, or `unknown` |
| `provider` | URL host, lower-cased. `other` once more than 32 endpoints have been seen |
| `method` | A name from the built-in method list, `other` for anything else, `batch` on the duration histogram for a batched request |
| `outcome` | One of the seven outcomes above |

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `rpc_xray_requests_total` | counter | `chain`, `provider`, `method`, `outcome` |
| `rpc_xray_request_duration_seconds` | histogram | `chain`, `provider`, `method` |
| `rpc_xray_slow_seconds_total` | counter | `chain`, `provider` |
| `rpc_xray_head_block` | gauge | `chain`, `provider` |
| `rpc_xray_head_last_advance_timestamp_seconds` | gauge | `chain`, `provider` |
| `rpc_xray_head_age_seconds` | gauge | `chain`, `provider` |
| `rpc_xray_head_lag_blocks` | gauge | `chain`, `provider` |
| `rpc_xray_probe_requests_total` | counter | `chain`, `provider`, `result` (`ok`, `error`) |
| `rpc_xray_value_per_request_usd` | gauge | none, only when configured |
| `rpc_xray_revenue_per_hour_usd` | gauge | none, only when configured |
| `rpc_xray_offers_enabled` | gauge | none |
| `rpc_xray_info` | gauge | `version`, `lang` |

- Histogram buckets, in seconds: 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10.
- Duration is measured from the call to the end of the inspected body, or to the response headers when the body is not inspected.
- `rpc_xray_slow_seconds_total` adds `duration - 1` for every request slower than 1 second.
- `rpc_xray_head_lag_blocks` is the highest head among the endpoints of the same chain minus this endpoint's head.

## Head probe

Each endpoint is polled with `eth_blockNumber` every `probeIntervalSeconds` (default 15, `0` turns the probe off). Probe calls are counted in `rpc_xray_probe_requests_total` only, never in `rpc_xray_requests_total`.
