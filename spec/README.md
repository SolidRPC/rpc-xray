# rpc-xray specification

Every language package implements this contract and passes `vectors.json`. Alert rules and dashboards depend only on what is written here.

## Pass-through rules

1. Request URLs, headers, and bodies are preserved. Adapters do not add retries or change timeouts. JavaScript returns the original promise and response. Python and Go preserve response values and errors while wrapping the body stream. Rust reqwest uses an explicit response adapter. The Alloy HTTP transport uses the existing reqwest client and preserves Alloy response packets and errors while retaining the HTTP status for metrics.
2. Telemetry parsing or recording failures do not replace application results. A poisoned Rust registry is skipped. Native runtime failures such as allocation failure or application panics are not intercepted.
3. No extra network reads are awaited to collect telemetry. JavaScript observes a cloned response asynchronously. Python, Go, and Rust reqwest observe bytes as the caller consumes them. Alloy observes the body read required by its HTTP transport before decoding it into packets. Bounded local parsing and counter updates add some CPU work to these native adapters.
4. The URL, method, headers and body of a request are never changed.
5. A response body is inspected only on a copy and only up to the configured body cap (default 1 MiB). A larger body is classified by HTTP status alone. Native stream adapters also use status alone if the caller closes before finishing or the observed bytes remain encoded. The Python requests adapter observes the chunks requests already decodes for the caller, including gzip, with the cap applied to decoded bytes. Its raw undecoded reads and HTTPX's encoded transport responses retain status-only classification. Request streams that cannot be copied without consumption are skipped. Metrics cannot infer a JSON-RPC error from an unread body.
6. Only JSON-RPC requests are recorded: a POST whose body is a JSON object or array carrying `method`.
7. No network calls are made except to the endpoints the application already calls.
8. Credentials never appear in metrics or logs. The `provider` label is the URL host, including any non-default port.

## Outcomes

One outcome per JSON-RPC call. A batch yields one outcome per entry.

| Outcome | Meaning |
|---|---|
| `ok` | HTTP 2xx or 3xx and no JSON-RPC error |
| `http_error` | HTTP 4xx or 5xx, except 429 |
| `rate_limited` | HTTP 429, or a JSON-RPC error that says the caller was throttled |
| `rpc_error` | HTTP 2xx carrying a JSON-RPC error that is the provider's fault, or a body that is not valid JSON-RPC |
| `caller_error` | HTTP 2xx carrying a JSON-RPC error caused by the request itself: a revert, invalid parameters, an unknown method, a rejected transaction |
| `timeout` | A transport operation was aborted or timed out, including an inspected response-body read in native adapters |
| `network` | A transport operation failed for another reason, including an inspected response-body read in native adapters |

Classification order for a JSON-RPC error object `{code, message}`:

1. `rate_limited` if `code` is 429, or the lower-cased message matches `rate limit`, `too many requests`, `throttl`, `capacity exceeded`, `quota exceeded` or `exceeded the quota`.
2. `caller_error` if `code` is one of 3, -32600, -32601, -32602, -32700, or the lower-cased message contains one of: `execution reverted`, `revert`, `nonce too low`, `nonce too high`, `already known`, `insufficient funds`, `underpriced`, `intrinsic gas too low`, `gas required exceeds`, `out of gas`, `exceeds block gas limit`, `max fee per gas less than block base fee`, `invalid opcode`, `invalid sender`, `invalid params`, `method not found`, `does not exist/is not available`.
3. `rpc_error` otherwise.

HTTP status takes precedence over JSON-RPC content. HTTP 429 is `rate_limited` even when its body contains a caller error. Other HTTP 4xx and 5xx responses are `http_error` even when the body is valid JSON-RPC. The Alloy HTTP transport observes status before Alloy's packet decoding can discard it.

Batches: each request entry takes the outcome of the response entry with the same `id`. An entry with no matching response is `rpc_error`. If the response to a batch is a single object, every entry takes its outcome. Request counters count logical JSON-RPC items, while the duration histogram counts HTTP exchanges. One HTTP batch containing three calls adds three request observations and one duration observation.

## Labels

| Label | Values |
|---|---|
| `chain` | A name from the built-in chain list, `eip155:<id>` for other chains, or `unknown` |
| `provider` | URL host, lower-cased, including any non-default port. `other` once more than 32 endpoints have been seen |
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

JavaScript polls each endpoint with `eth_blockNumber` every `probeIntervalSeconds` (default 15, `0` turns the probe off). Supply an explicit chain as well to disable its chain discovery calls.

Python, Go, and Rust are passive and never schedule probes or discover chains. Applications can record results from their own separate, unwrapped head probes with `record_probe` / `RecordProbe`. A failed probe increments the `error` result and leaves the last known head intact. A successful head higher than the previous head updates the last-advance timestamp. Probe calls are counted only in `rpc_xray_probe_requests_total`. Without successful observations, head gauges are absent. Native SDKs aggregate head observations by chain and provider host, so use distinct hosts if individual routes need independent head measurements.

Native SDKs export `rpc_xray_offers_enabled 0` and do not print promotional messages. Their `rpc_xray_info` language values are `python`, `go`, and `rust`. JavaScript retains `js` and its configurable offer behavior. Each SDK reports its own package version.

## Shared sources and conformance

`contract.json` is the source for chain names, method names, histogram buckets, caller codes, and error patterns. `scripts/generate-contract.py` writes the language copies. Run it after contract edits and commit the results together. CI checks that generated files match.

`vectors.json` is run by all four SDKs. Each implementation also tests transport behavior, bounded capture, and metric output. Real-client tests cover web3.py compressed responses, Go client timeouts, and Alloy HTTP status precedence. The complete HTTP, go-ethereum, and Alloy examples use only local mock RPCs in CI. They verify `/metrics`, errors and recovery, pacing, credential omission, and shutdown.
