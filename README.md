# rpc-xray

**See every request your RPC provider failed.**

Add three lines of code. rpc-xray counts the errors, stale blocks and slow calls your users hit, and shows what they have cost you so far.

![A red line of failed user requests climbs while a flat green line shows where it should be](assets/hero.png)

Your requests pass through untouched. rpc-xray measures each one, exposes Prometheus metrics, and ships a Grafana dashboard, alert rules and Slack incident messages that work as they are.

## Install

```bash
npm install @solidrpc/rpc-xray
```

```ts
import { xray, serveMetrics } from "@solidrpc/rpc-xray";

const client = createPublicClient({ transport: http(url, { fetchFn: xray(fetch) }) });
await serveMetrics();
```

That is the whole integration for viem. ethers and plain `fetch` are in the [JavaScript README](js/README.md). Go and Rust packages follow the same [specification](spec/README.md) and are next.

## Add it with a coding agent

Paste this prompt into your coding agent, in your project:

```text
Add rpc-xray to this project. rpc-xray is a pass-through wrapper that measures
EVM JSON-RPC requests. It must not change how any request behaves.

1. Find every place this codebase creates an EVM JSON-RPC client, provider or
   transport over HTTP: viem, ethers, web3.js, go-ethereum, alloy, or a plain
   HTTP client that posts JSON-RPC.
2. Read the rpc-xray README for this language at
   https://github.com/SolidRPC/rpc-xray and install the package with the
   package manager this project already uses.
3. Wrap the HTTP layer of each client with rpc-xray. Do not change URLs,
   headers, retries, timeouts, or any call site that uses the client.
4. Expose the metrics once per process. If the project already runs an HTTP
   server, mount the rpc-xray metrics handler at /metrics. Otherwise start the
   built-in metrics server on port 9464.
5. Do not write API keys, endpoint URLs or other secrets into code or logs.
6. Run the project's type check and tests. Fix only what your change broke.
7. Report the files you changed, where the metrics are exposed, and any RPC
   client you could not wrap and why.
```

## See it in five minutes

No monitoring stack yet? This starts Prometheus, Alertmanager and Grafana with everything loaded:

```bash
git clone https://github.com/SolidRPC/rpc-xray && cd rpc-xray/stack
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/... docker compose up -d
```

Open http://localhost:3000 (set `GRAFANA_PORT` if 3000 is taken). The stack scrapes your app at `host.docker.internal:9464`. Change `stack/targets.yml` to point somewhere else. Leave `SLACK_WEBHOOK_URL` unset to run without Slack.

To see the dashboard with sample traffic and scripted incidents before touching your own code:

```bash
(cd ../js && npm install && npm run build)
docker compose --profile demo up -d
```

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

One addition: a head probe calls `eth_blockNumber` on each endpoint every 15 seconds, about 173,000 requests a month per endpoint. Staleness and lag cannot be seen without it. Change it with `probeIntervalSeconds`, or set it to `0` to turn it off.

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

## License

Apache-2.0
