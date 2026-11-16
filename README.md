# rpc-xray

**See every request your RPC provider failed.**

Add three lines of code. rpc-xray counts the errors, stale blocks and slow calls your users hit, and shows what they have cost you so far.

![A red line of failed user requests climbs while a flat green line shows where it should be](assets/hero.png)

Your requests pass through untouched. rpc-xray measures each one, exposes Prometheus metrics, and ships a Grafana dashboard, alert rules and Slack incident messages that work as they are.

## Install

Node.js 20 or newer. The package has no runtime dependencies.

```bash
npm install @solidrpc/rpc-xray
```

This is the standard installation path once the package is published. For testing before the first npm release, use the [development instructions](#development-before-publishing).

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

Here `url` is your existing endpoint. The explicit chain label and disabled probes keep instrumentation from adding RPC calls. Omit these options to enable chain detection and the default head probes. See [request overhead](#what-it-does-to-your-requests) before choosing.

Start with the [complete plain Node.js example](examples/plain-node/README.md) for a runnable app, `/metrics`, shutdown handling and matching Prometheus configuration. [Other JavaScript integrations](js/README.md) cover ethers and existing HTTP servers. Go and Rust packages are planned and are not available yet.

## Add it with a coding agent

Paste this prompt into your coding agent, in your project:

```text
Integrate RPC X-Ray using https://github.com/SolidRPC/rpc-xray.

1. Read the root and JavaScript READMEs. Find the existing HTTP JSON-RPC
   clients. JavaScript is implemented. Report unsupported languages.
2. Install @solidrpc/rpc-xray with the existing package manager. If the
   requested version is not published yet, report that release prerequisite.
   The documented local tarball is a fallback for pre-release testing.
3. Wrap the existing HTTP layer without changing URLs, headers, retries,
   timeouts or application call sites. Keep credentials out of code and logs.
4. Respect the application's request budget. To add zero RPC calls, set
   probeIntervalSeconds: 0 AND supply the correct chain for every endpoint.
   Explain that head freshness and lag are then unmeasured. Otherwise document
   the eth_chainId detection calls and eth_blockNumber probe overhead.
5. Expose GET /metrics once per process on the existing HTTP server, or use
   serveMetrics on port 9464. Use the same registry for wrapping and exporting.
   An Xray instance exports with instance.metrics(), not the shared handler.
6. Set the Prometheus host, port and metrics_path to the actual endpoint.
   For Docker, use host.docker.internal to reach the host, not localhost.
   Verify the target is UP and metrics advance after real application calls.
7. Run type checks and tests. Use local mock RPCs for integration tests.
   Check healthy traffic with no error series, missing head measurements,
   and the no-incident dashboard state. Do not turn missing data into health.
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
- Lag needs two or more endpoints for the same chain. With one endpoint, staleness is still detected.

## What waiting costs

The dashboard has a row that only goes one way:

- Failed user requests, adding up since the start of the time range, against the line where the same traffic should be.
- Total time users waited beyond 1 second.
- Minutes with an open incident.
- This week against last week, and a straight-line projection for the next 30 and 90 days.
- Estimated loss in dollars, shown only if you set `valuePerRequest`. rpc-xray never invents a money figure.

The targets on the dashboard are editable at the top: success rate and p95 latency.

## What it does to your requests

Nothing. These rules are tested:

- The wrapped client returns exactly what the underlying client returns: the same promise, the same response, the same error.
- Any failure inside rpc-xray is swallowed and never reaches your code.
- Recording happens after your code has the response. Nothing is awaited on the request path.
- URLs, headers, bodies, retries and timeouts are never changed.
- Response bodies are read on a copy and only up to 1 MiB. Larger answers are classified by HTTP status.
- API keys never reach metrics or logs. The provider label is the host name only.
- rpc-xray makes no network calls except to the endpoints you already use. Nothing is sent to SolidRPC.

By default, RPC X-Ray detects each endpoint's chain with `eth_chainId` (up to three attempts) and probes `eth_blockNumber` immediately after discovery, then every 15 seconds, about 173,000 probe requests per 30-day month per endpoint. Change the probe interval with `probeIntervalSeconds`.

For **zero additional RPC calls**, set `probeIntervalSeconds: 0` and supply an explicit `chain` to every wrapper. Disabling probes alone does not disable chain detection. Request counters and latency remain available. Head age and lag are unmeasured, and the dashboard must not be read as reporting zero lag. Scraping `/metrics` itself never triggers RPC calls.

## The SolidRPC offer

rpc-xray is built by [SolidRPC](https://solidrpc.io). If you send about 100M requests a month and rpc-xray recorded incidents in the last 7 days, the dashboard, one Slack message and one log line will tell you that SolidRPC offers a 14-day free trial or $100 credit. To take it, email hello@solidrpc.io.

Nothing is sent to us. Set `offers: false` to turn the message off.

## Configuration

Everything is optional.

| Option | Default | Meaning |
|---|---|---|
| `chain` | detected with `eth_chainId` | Chain label for this wrapped client |
| `probeIntervalSeconds` | `15` | Head probe interval. `0` turns it off |
| `maxBodyBytes` | `1048576` | Largest response body that is inspected |
| `valuePerRequest` | not set | Value of one request in USD, for the loss estimate |
| `revenuePerHour` | not set | Revenue per hour in USD, exported for your own panels |
| `offers` | `true` | Show the SolidRPC offer when eligible |
| `logger` | `console.info` | Where the single offer line is written |

## Development before publishing

Maintainers can prepare and publish releases with the [manual npm release workflow](RELEASING.md). Its default mode validates and packages without publishing.

These steps are for contributors and pre-release testing. Application integrations use the npm package and import `@solidrpc/rpc-xray`.

Before the first npm release, build a local package from a checkout. Repository access is required while the repository is private:

```bash
git clone https://github.com/SolidRPC/rpc-xray.git
cd rpc-xray/js
npm ci
npm pack
```

`npm pack` builds the package and creates `solidrpc-rpc-xray-0.1.0.tgz`, including its license. Install that tarball in a test app using its actual path:

```bash
npm install --no-save --package-lock=false /absolute/path/to/rpc-xray/js/solidrpc-rpc-xray-0.1.0.tgz
```

The application import stays `@solidrpc/rpc-xray`. The [plain Node example](examples/plain-node/README.md#test-before-publication) and CI exercise this packaged build without requiring a published release.

## License

Apache-2.0
