# rpc-xray

**See every request your RPC provider failed.**

Instrument your existing RPC client to see which methods drive traffic, when an endpoint slows down, and which calls fail even though the server returned HTTP 200.

![Grafana shows request counts, traffic by RPC method, and a p95 latency spike on the archive endpoint](assets/dashboard.png)

In this simulated Ethereum workload, `eth_getLogs` traffic rose around 10:25 UTC. The archive endpoint’s p95 response time peaked at 1.37 seconds. The primary stayed near 100 ms, below the 300 ms target.

Your requests pass through untouched. rpc-xray measures each one, exposes Prometheus metrics, and ships a Grafana dashboard, alert rules and Slack incident messages that work as they are.

## Install

Node.js 20 or newer. The package has no runtime dependencies.

```bash
npm install @solidrpc/rpc-xray
```

The [SDK table](#language-sdks) lists installation options for all four languages.

For an existing viem app:

```ts
import { createPublicClient, http } from "viem";
import { xray, serveMetrics } from "@solidrpc/rpc-xray";

const client = createPublicClient({
  transport: http(url, {
    fetchFn: xray(fetch, { chain: "ethereum", probeIntervalSeconds: 0 }),
  }),
});
await serveMetrics({ host: "127.0.0.1", port: 9464 });
```

Here `url` is your existing endpoint. The explicit chain label and disabled probes keep instrumentation from adding RPC calls. With default options, a successful first call to a new endpoint normally makes three RPC requests in total (the application call, one chain-ID lookup and one head probe). The SDK then probes every 15 seconds. Disabling probes alone still permits the chain lookup. [Request overhead](#what-it-does-to-your-requests)

Start with the [complete plain Node.js example](examples/plain-node/README.md) for a runnable app, `/metrics`, shutdown handling, and matching Prometheus configuration. [Other JavaScript integrations](js/README.md) cover ethers and existing HTTP servers.

## Language SDKs

All four SDKs use the same metric names, labels, histogram buckets, and error classifications. They share the dashboards and alert rules.

| Language | Supported integration | Package | Complete example |
|---|---|---|---|
| [JavaScript / TypeScript](js/README.md) | fetch, viem, ethers | [@solidrpc/rpc-xray 0.1.3 on npm](https://www.npmjs.com/package/@solidrpc/rpc-xray/v/0.1.3) | [Node.js](examples/plain-node/README.md) |
| [Python](python/README.md) | HTTPX sync and async, requests, web3.py HTTPProvider | [solidrpc-rpc-xray 0.1.1 on PyPI](https://pypi.org/project/solidrpc-rpc-xray/0.1.1/) | [Python](examples/plain-python/README.md) |
| [Go](go/README.md) | `net/http`, `go-ethereum` | [github.com/SolidRPC/rpc-xray/go v0.1.1](https://pkg.go.dev/github.com/SolidRPC/rpc-xray/go@v0.1.1) | [HTTP](examples/plain-go/README.md), [go-ethereum](examples/go-ethereum/README.md) |
| [Rust](rust/README.md) | reqwest 0.13, Alloy 2.5 | [solidrpc-rpc-xray 0.1.1 on crates.io](https://crates.io/crates/solidrpc-rpc-xray/0.1.1) | [reqwest](examples/plain-rust/README.md), [Alloy](examples/alloy/README.md) |

The SDK guides and examples use the public registries. Every example has `/metrics`, shutdown handling, and a matching Prometheus config. Run one example at a time on the default port 9464, or assign separate ports and scrape targets.

Python, Go, and Rust add no RPC calls of their own. They read the chain head from successful `eth_blockNumber` and `eth_getBlockByNumber("latest", …)` responses your application already receives, so a stale or lagging provider shows up as soon as your app polls the head. Supply a chain label explicitly. If your app never reads the latest head, record a separate probe with `record_probe` or `RecordProbe`. JavaScript detects chains and probes heads itself, described below.

## Add it with a coding agent

Paste this prompt into your coding agent, in your project:

```text
Integrate RPC X-Ray using https://github.com/SolidRPC/rpc-xray.

1. Read the root README and the README for the application's language.
   Find the existing HTTP JSON-RPC clients and a supported adapter.
2. Install the published package from the language’s registry with the existing
   package manager. Use a local package only when testing development changes.
3. Use the supported HTTP adapter without changing URLs, headers, retries,
   or timeouts. Explain any required call-site changes, including Rust's
   explicit reqwest response wrapper. Keep credentials out of code and logs.
4. Respect the application's request budget. In JavaScript, to add zero RPC calls, set
   probeIntervalSeconds: 0 AND supply the correct chain for every endpoint.
   Use existing latest-head reads for freshness. Otherwise document
   the eth_chainId detection calls and eth_blockNumber probe overhead.
   Python, Go, and Rust add no RPC calls and require explicit chain labels.
   They measure head freshness from the app’s own eth_blockNumber and latest-block answers.
5. Expose GET /metrics once per process using the same registry that wraps
   the client. In JavaScript, use serveMetrics for the shared registry or
   export your Xray instance's metrics(). Python and Rust export
   telemetry.metrics() through the application's HTTP server. Go exposes
   telemetry.Handler(). Follow the language's complete server example.
6. Set the Prometheus host, port and metrics_path to the actual endpoint.
   For Docker, use host.docker.internal to reach the host, not localhost.
   Verify the target is UP and metrics advance after real application calls.
7. Run type checks and tests through the actual application client. Use
   local mock RPCs for failures, including compressed JSON-RPC errors,
   client timeouts, HTTP 429/500 with JSON-RPC bodies, and mixed batches.
   Check healthy traffic, missing head measurements, and no-incident
   dashboard states. Do not turn missing data into health. Verify that
   scraping metrics adds zero RPC calls and stop test apps afterward.
8. Report changed files, metrics URL, added RPC overhead, how to stop any
   example process, and clients that could not be wrapped.
```

## Start the monitoring stack

No monitoring stack yet? This starts Prometheus, Alertmanager and Grafana with everything loaded:

```bash
git clone https://github.com/SolidRPC/rpc-xray && cd rpc-xray/stack
docker compose up -d
```

Open [Grafana](http://localhost:3000) (set `GRAFANA_PORT` if 3000 is taken). The stack scrapes your app at `host.docker.internal:9464/metrics`. Start your instrumented app first, then confirm the target is **UP** at [Prometheus targets](http://localhost:9090/targets). Change `stack/targets.yml` when your metrics port differs. If your metrics path differs, also set `metrics_path` in `stack/prometheus.yml`. Do not put RPC URLs or API keys in either file.

Docker Desktop reaches the Mac or Windows host through `host.docker.internal`. On native Linux, bind the app metrics server to a Docker-reachable interface and restrict access, since the host gateway cannot reach loopback. See the [plain Node example](examples/plain-node/README.md#scrape-with-prometheus).

Leave `SLACK_WEBHOOK_URL` unset to run without notifications. To enable Slack, set it in an ignored `stack/.env` file and recreate Alertmanager with `docker compose up -d --force-recreate alertmanager`.

To see the dashboard with sample traffic and scripted incidents before touching your own code:

```bash
(cd ../js && npm ci && npm run build)
RPC_XRAY_TARGETS=./targets.demo.yml docker compose --profile demo up -d
```

The demo uses only local mock servers. Its separate target file scrapes `demo:9464` instead of an absent host app. Stop it with `docker compose --profile demo down` before returning to the default app target. Stopping the monitoring stack does not stop your application or its RPC requests.

Open [Prometheus alerts](http://localhost:9090/alerts) to see the five incident rules. Expand a rule to inspect its threshold, hold time and provider labels. [Alertmanager](http://localhost:9093) shows the receiver for each firing alert. Slack incident notifications include a recovery message with the duration. Message titles link to Alertmanager on localhost. When teammates need to open them, set `ALERTMANAGER_EXTERNAL_URL` to their reachable Alertmanager URL and recreate the service.

Allow two scrapes plus a recording-rule evaluation for rate panels. Rollups first appear at the next five-minute evaluation. Weekly comparisons need accumulated history. No data is not evidence of a healthy endpoint.

Already running Prometheus? Take the files you need:

| File | Use |
|---|---|
| [`alerts/rpc-xray.rules.yml`](alerts/rpc-xray.rules.yml) | Recording and alert rules for Prometheus |
| [`alerts/rpc-xray.prometheusrule.yaml`](alerts/rpc-xray.prometheusrule.yaml) | The same rules for the Prometheus Operator |
| [`alerts/alertmanager.yml`](alerts/alertmanager.yml) | Alertmanager route and Slack messages |
| [`dashboards/rpc-xray.json`](dashboards/rpc-xray.json) | Grafana dashboard |

## What you will see in the first hour

- Your success rate on real traffic, next to where it should be.
- Every failed request, split by kind: HTTP errors, rate limits, timeouts, and errors hidden inside HTTP 200 answers.
- How old each provider's latest block is, and how far it lags behind your other providers.
- A Slack message when an incident opens and another when it ends.

## Incidents

An incident is something a person would have been paged for. Defaults, per chain and provider:

| Incident | Opens when |
|---|---|
| Stale | No new block for 3 minutes |
| Lagging | More than 10 seconds of blocks behind your best source, for 3 minutes |
| Failing requests | More than 5% of requests over 3 minutes return HTTP 4xx or 5xx, are rate limited, time out or never connect |
| Errors at HTTP 200 | More than 5% of requests over 3 minutes return HTTP 200 with a provider error inside |
| Slow | p95 latency above 1 second for 3 minutes |

- Reverts, invalid parameters and rejected transactions are your own results. They are counted as `caller_error` and never open an incident.
- A null receipt, an empty log list and a zero balance are valid answers. They count as `ok`.
- Error-rate incidents need at least a little traffic, so one failed call on a quiet endpoint does not page anyone.
- With one endpoint, lag is zero and head age measures staleness. With two or more endpoints on the same chain, lag shows which one is behind.

## Read the dashboard

![Grafana shows the archive endpoint’s rolling success rate dropping to 96% as JSON-RPC errors, rate limits, and HTTP errors rise](assets/provider-failures.png)

During the same simulated incident, the archive endpoint’s rolling one-hour success rate fell to about 96%. JSON-RPC errors inside HTTP 200 responses were the largest failure category. Monitoring HTTP status alone would have missed them.

Select a chain and provider to see its firing and pending incidents and the rules that fired during the selected range. **No samples** means no matching request telemetry, **Awaiting rules** means requests arrived before the first incident-rule sample, and **Quiet** means the rules evaluated with no firing incidents.

The request overview counts requests from the selected SDK processes at the end of the time range. A failure first scraped at a count of one appears immediately. Counters reset when a process restarts. Provider success includes valid caller rejections, which also appear under **Caller errors**.

Traffic, latency and failure-rate charts follow the time picker. Prometheus needs at least two scrapes to estimate a rate or increase. Head freshness needs recent head observations, and peer lag needs two measured providers on one chain in the same registry.

The impact row estimates excess wait and the value of failed calls over the selected range. Set a value per request on each measured instance to include it in the dollar estimate. Edit the success and p95 targets at the top to change the chart reference lines.

The fleet row covers all providers and chains in the selected datasource. It uses five-minute samples for weekly failures, projections and incident minutes. Weekly comparisons and forecasts need a week of history.

## What it does to your requests

The JavaScript fetch wrapper keeps its original pass-through behavior:

- The wrapped client returns exactly what the underlying client returns: the same promise, the same response, the same error.
- Any failure inside rpc-xray is swallowed and never reaches your code.
- Recording happens after your code has the response. Nothing is awaited on the request path.
- URLs, headers, bodies, retries and timeouts are never changed.
- Response bodies are read on a copy and only up to 1 MiB. Larger answers are classified by HTTP status.
- API keys never reach metrics or logs. The provider label contains only the host and any non-default port.
- rpc-xray makes no network calls except to the endpoints you already use. Nothing is sent to SolidRPC.

Python and Go wrap HTTP transports. They return the underlying response while observing bounded copies as your code reads its body. Rust provides an explicit reqwest response wrapper and an Alloy HTTP transport that observes the original status before decoding packets. These adapters do not add body reads, retries, or deadline changes. Read each SDK's body-consumption notes before using streaming clients. Metrics are available after consumption or closure. The shared [specification](spec/README.md) describes these language boundaries.

By default, the JavaScript SDK detects each endpoint's chain with `eth_chainId` (up to three attempts) and probes `eth_blockNumber` immediately after discovery, then every 15 seconds, about 173,000 probe requests per 30-day month per endpoint. Change the probe interval with `probeIntervalSeconds`.

For **zero additional RPC calls**, set `probeIntervalSeconds: 0` and supply an explicit `chain` to every wrapper. Disabling probes alone does not disable chain detection. Request counters and latency remain available. The wrapper measures head age from matching block-number and latest-block responses while observations remain fresh. The dashboard needs two measured providers on the same chain to show peer lag. Scraping `/metrics` itself never triggers RPC calls.

## Configuration

The following options apply to JavaScript. See the language SDK READMEs for Python, Go, and Rust options.

| Option | Default | Meaning |
|---|---|---|
| `chain` | detected with `eth_chainId` | Chain label for this wrapped client |
| `probeIntervalSeconds` | `15` | Head probe interval. `0` turns it off |
| `maxBodyBytes` | `1048576` | Largest response body that is inspected |
| `valuePerRequest` | not set | Value of one request in USD, for the loss estimate |
| `revenuePerHour` | not set | Revenue per hour in USD, exported for your own panels |
| `offers` | `true` | Set to `false` to disable optional offer messages |
| `logger` | `console.info` | Where the single offer line is written |

## Development and release testing

Maintainers can prepare and publish releases with the [manual npm release workflow](RELEASING.md). Its default mode validates and packages without publishing.

Python, Go, and Rust are versioned independently. Their CI jobs validate packages and examples but do not publish or create release tags. Release preparation for these languages is documented in [RELEASING.md](RELEASING.md#python-go-and-rust).

The allowlists, buckets, and error patterns live in `spec/contract.json`. After changing it, run `python3 scripts/generate-contract.py`. All four languages run `spec/vectors.json` in CI. Generated copies are checked for drift.

These steps are for contributors and pre-release testing. Application integrations use the npm package and import `@solidrpc/rpc-xray`.

To test changes before a release, build a local package from a checkout:

```bash
git clone https://github.com/SolidRPC/rpc-xray.git
cd rpc-xray/js
npm ci
npm pack
```

`npm pack` builds the package and creates `solidrpc-rpc-xray-0.1.3.tgz`, including its license. Install that tarball in a test app using its actual path:

```bash
npm install --no-save --package-lock=false /absolute/path/to/rpc-xray/js/solidrpc-rpc-xray-0.1.3.tgz
```

The application import stays `@solidrpc/rpc-xray`. The [plain Node example](examples/plain-node/README.md#test-before-publication) and CI exercise this packaged build without requiring a published release.

## License

Apache-2.0
