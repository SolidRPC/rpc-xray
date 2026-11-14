# Plain Node.js with Prometheus

A complete `node:http` app using built-in `fetch`. It reads `eth_blockNumber` from one endpoint at most once per second, serves the latest result at `/`, and exports RPC X-Ray metrics at `/metrics` on the same port. It needs no application dependencies or framework.

Use Node.js 20.6 or newer and Docker Compose for Prometheus. Repository access is required while the repository is private. This example imports the locally built package, so it also works before the npm package is published.

## Build and run

From the repository root:

```bash
cd js
npm ci
npm run build
cd ../examples/plain-node
cp .env.example .env
chmod 600 .env
```

Edit `.env`. Set `RPC_URL` to your complete endpoint and `CHAIN` to its label, such as `ethereum`, `base`, or `bsc`. Keep credentials in this ignored file. The chain label is supplied by you, not validated against the endpoint.

```bash
node --env-file=.env server.mjs
```

In another terminal:

```bash
curl http://localhost:9464/
curl http://localhost:9464/metrics
```

Expect a JSON block result at `/` and a counter such as `rpc_xray_requests_total{chain="ethereum",...outcome="ok"} 1` at `/metrics` after the first response. Metrics contain the endpoint host, not its path or query string. Errors appear as `status: "error"` at `/` without exposing upstream messages or credentials.

The wrapper uses `probeIntervalSeconds: 0` **and** an explicit `chain`. Together they disable all additional RPC calls from RPC X-Ray. Scraping `/metrics` does not send an RPC request. This app owns its one-second polling timer, skips a tick while a request is pending, applies a 4.5-second timeout, and does not retry. Head freshness and lag metrics are unmeasured in this mode, even though the app itself reads block numbers.

Press **Ctrl+C** to stop the app. Shutdown clears polling, aborts the pending request, closes telemetry timers, and closes the HTTP server.

## Scrape with Prometheus

From this directory, in another terminal:

```bash
docker compose up -d
```

The matching [`prometheus.yml`](prometheus.yml) scrapes `http://host.docker.internal:9464/metrics` every 15 seconds and loads the repository recording and alert rules. It connects to the Mac or Windows host through Docker Desktop. The published Prometheus port is local only.

Open [Prometheus targets](http://localhost:9090/targets) and confirm `rpc-xray-plain-node` is **UP**. After at least two scrapes, query:

```promql
sum by (chain, provider) (rate(rpc_xray_requests_total[1m]))
```

The result approaches 1 request per second when the endpoint responds promptly. The `rpc_xray:success_ratio:rate1h` recording rule should read `1` when all observed requests succeed. It does not require a full hour of history, but it does need two counter samples and a rule evaluation. No traffic is not a success measurement.

If port 9090 is occupied, use `PROMETHEUS_PORT=9091 docker compose up -d` and open port 9091. If you change the app's `PORT`, change the target port in `prometheus.yml` too.

On native Linux, the host gateway cannot reach a service bound only to loopback. Set `HOST=0.0.0.0` or bind to an interface reachable from Docker and restrict access with your firewall. This example has no HTTP authentication. A native Prometheus process on the same host should instead scrape `127.0.0.1:9464` and use an absolute local path for `rule_files`.

To use the full Grafana and Alertmanager stack, run `docker compose up -d` in `../../stack` **instead** of starting this example's Prometheus. Its default target is also `host.docker.internal:9464`. Open [Grafana](http://localhost:3000). Leave Slack unset for local inspection without notifications.

To stop Prometheus:

```bash
docker compose down
```

Stopping Prometheus does not stop the Node.js app. Use Ctrl+C in the app terminal to stop RPC traffic.

## Integrate into an existing app

Install the package using the [source installation instructions](../../README.md#install), then change the import in `server.mjs` to `import { Xray } from "@solidrpc/rpc-xray"`. Wrap your existing `fetch` and expose that same instance's `telemetry.metrics()` through your server. Keep the app's existing polling, retry and timeout policy. The polling loop here is sample application behavior, not a requirement of the wrapper.

The convenience exports `xray()`, `metrics()` and `metricsHandler()` share a default registry. A `new Xray()` instance has its own registry. Do not mix an instance wrapper with the default metrics handler or you will scrape the wrong registry.

## Test without real RPC traffic

After building `js`, run from the repository root:

```bash
node --test examples/plain-node/server.test.mjs
```

The tests use a local mock JSON-RPC server. They verify metrics, request counts, credential omission and shutdown without contacting a blockchain provider.
