# rpc-xray specification

Every language package implements this contract and passes `vectors.json`. Alert rules and dashboards depend only on what is written here.

## Pass-through rules

1. Request URLs, headers, and bodies are preserved. Adapters do not add retries or change timeouts. JavaScript returns the original promise and response. Python and Go preserve response values and errors while wrapping the body stream. Rust reqwest uses an explicit response adapter. The Alloy HTTP transport uses the existing reqwest client and preserves Alloy response packets and errors while retaining the HTTP status for metrics.
2. Telemetry parsing or recording failures do not replace application results. A poisoned Rust registry is skipped. Native runtime failures such as allocation failure or application panics are not intercepted.
3. No extra network reads are awaited to collect telemetry. JavaScript observes a cloned response asynchronously. Python, Go, and Rust reqwest observe bytes as the caller consumes them. Alloy observes the body read required by its HTTP transport before decoding it into packets. Bounded local parsing and counter updates add some CPU work to these native adapters.
4. The URL, method, headers and body of a request are never changed.
5. A response body is inspected only on a copy and only up to the configured body cap (default 1 MiB). A larger body is classified by HTTP status alone. Native stream adapters also use status alone if the caller closes before finishing or the observed bytes remain encoded. Python's requests and HTTPX sync/async adapters observe bytes their clients already decode for the caller, with the cap applied to decoded bytes. Compressed raw reads that bypass client decoding retain status-only classification. HTTPX content-decoding errors during inspection are `rpc_error`, with the original exception preserved. Request streams that cannot be copied without consumption are skipped. Metrics cannot infer a JSON-RPC error from an unread body.
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

Batches: each request entry takes the outcome of the response entry with the same `id`. An entry with no matching response is `rpc_error`. A single error object with a null or missing ID rejects the whole batch, so each entry takes that error outcome. Any other singleton response to a batch is malformed and every entry receives `rpc_error`. Request counters count logical JSON-RPC items, while the duration histogram counts HTTP exchanges. One HTTP batch containing three calls adds three request observations and one duration observation.

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

Python, Go, and Rust never schedule probes or discover chains. All four SDKs can read the head from application traffic. JavaScript does this when scheduled probes are disabled. A successful exchange can supply either a hexadecimal `eth_blockNumber` result or the `result.number` of an `eth_getBlockByNumber` call whose first parameter is `"latest"`. Both require an inspected body, HTTP status below 400, a matching non-null response ID, no error, and a `0x`-prefixed height of 1 to 16 hex digits. JavaScript limits heights and numeric IDs to safe integers. The highest matching head in a batch wins. Passive reads do not increment `rpc_xray_probe_requests_total`.

Python, Go and Rust applications can also record results from separate unwrapped probes with `record_probe` / `RecordProbe`. A failed probe increments the `error` result and leaves the last known head intact. Probe calls are counted only in `rpc_xray_probe_requests_total`.

The head gauge follows the latest observation, including a lower height after a rollback. Only an increase from the immediately previous observation updates the last-advance timestamp after initialization. Every head observation refreshes a last-seen time. In passive mode SDKs export the four head gauges only for endpoints observed within the last 120 seconds, and compute lag among those endpoints only. This keeps an application that stops polling from raising a false stale-head incident, while an application that keeps polling a frozen provider still sees the head age grow. Without recent observations, head gauges are absent. Native SDKs aggregate head observations by chain and provider host, so use distinct hosts if individual routes need independent head measurements.

## Offer

All four SDKs export `rpc_xray_offers_enabled 1` unless the application turns offers off (`offers: false` in JavaScript and Rust, `offers=False` in Python, `DisableOffers: true` in Go). The `RpcXrayTrialOffer` alert requires this gauge to be `1`.

Each SDK also writes at most one log line per process. The check runs once per 10,000 recorded calls and needs all of: at least 24 hours since the registry was created, a projected 30-day volume of at least 100M calls, and either at least 1% of calls in a failure outcome (`http_error`, `rate_limited`, `rpc_error`, `timeout`, `network`) or a stale head seen (an observation where the head had not advanced for more than 180 seconds). The line names the 14-day free trial or $100 credit and hello@solidrpc.io, and says how to turn it off. JavaScript writes it with `console.info`, Python to the `rpc_xray` logger at WARNING, Go through the standard `log` package, and Rust to stderr. Each accepts a custom logger. A logger failure never reaches the application. Nothing is sent to SolidRPC.

`rpc_xray_info` language values are `js`, `python`, `go`, and `rust`. Each SDK reports its own package version.

## Shared sources and conformance

`contract.json` is the source for chain names, method names, histogram buckets, caller codes, and error patterns. `scripts/generate-contract.py` writes the language copies. Run it after contract edits and commit the results together. CI checks that generated files match.

`vectors.json` is run by all four SDKs. Each implementation also tests transport behavior, bounded capture, and metric output. Real-client tests cover web3.py and HTTPX sync/async compressed responses, Go client timeouts, and Alloy HTTP status precedence. Python CI tests the minimum HTTPX version and the latest compatible release, including the installed wheel's compression tests. The complete HTTP, go-ethereum, and Alloy examples use only local mock RPCs in CI. They verify `/metrics`, errors and recovery, pacing, credential omission, and shutdown.
