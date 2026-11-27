# @solidrpc/rpc-xray

**See every request your RPC provider failed.**

A pass-through wrapper for JavaScript and TypeScript. It measures your real EVM JSON-RPC traffic and exposes Prometheus metrics. Dashboards, alert rules and Slack incident messages are in the [main repository](https://github.com/SolidRPC/rpc-xray).

Node.js 20 or newer. No runtime dependencies.

## Install

```bash
npm install @solidrpc/rpc-xray
```

To test unreleased changes locally, see the [development instructions](https://github.com/SolidRPC/rpc-xray#development-and-release-testing). They build a local npm tarball without changing the application's package import.

For a runnable app without a framework, use the [complete plain Node.js example](https://github.com/SolidRPC/rpc-xray/tree/main/examples/plain-node). It includes `/metrics`, a matching Prometheus config, a local mock test and graceful shutdown.

## viem

```ts
import { createPublicClient, http } from "viem";
import { xray, serveMetrics } from "@solidrpc/rpc-xray";

const client = createPublicClient({ transport: http(url, { fetchFn: xray(fetch) }) });
await serveMetrics();
```

## ethers v6

```ts
import { FetchRequest, JsonRpcProvider } from "ethers";
import { xrayEthers, serveMetrics } from "@solidrpc/rpc-xray";

const request = new FetchRequest(url);
request.getUrlFunc = xrayEthers();
const provider = new JsonRpcProvider(request);
await serveMetrics();
```

`xrayEthers()` sends the request with the platform `fetch`. Everything else about the provider stays as it was.

## Plain fetch

```ts
import { xray } from "@solidrpc/rpc-xray";

const rpcFetch = xray(fetch);
const response = await rpcFetch(url, { method: "POST", body: JSON.stringify(call) });
```

Requests that are not JSON-RPC pass through and are not recorded.

## Metrics

For a separate server, import and call `serveMetrics({ host: "127.0.0.1", port: 9464 })`. It starts an unreferenced server, so it does not keep an otherwise idle process alive. Your app must own its normal lifecycle.

For an existing Node HTTP server:

```js
import { createServer } from "node:http";
import { metricsHandler } from "@solidrpc/rpc-xray";

const handleMetrics = metricsHandler();
createServer((request, response) => {
  if (request.method === "GET" && request.url === "/metrics") {
    handleMetrics(request, response);
    return;
  }
  response.writeHead(404);
  response.end("Not found");
}).listen(9464, "127.0.0.1");
```

`metricsHandler()` does not route requests itself. In Express, mount it with `app.get("/metrics", metricsHandler())`. In Fastify, use `reply.hijack()` and pass `request.raw` and `reply.raw` to the handler. For a fetch-style router, import `metricsResponse` and return `metricsResponse()` from your GET `/metrics` route. Import `metrics` to obtain the raw Prometheus text.

These convenience exports share the registry used by `xray()` and `xrayEthers()`. With `new Xray()`, expose that instance's `metrics()` instead. Do not mix registries. A scrape of the wrong registry may succeed with no application counters.

Prometheus must target the server's actual host, port and path. A Docker scraper uses `host.docker.internal:9464` to reach the host, not `localhost:9464`. See the runnable example for a complete configuration and Linux networking details.

## Options

Pass options as the second argument: `xray(fetch, { chain: "base", offers: false })`. The full list is in the [main README](https://github.com/SolidRPC/rpc-xray#configuration).

## Request budget and lifecycle

For no additional RPC traffic:

```js
import { Xray } from "@solidrpc/rpc-xray";

const telemetry = new Xray({ probeIntervalSeconds: 0, offers: false });
const rpcFetch = telemetry.wrap(fetch, { chain: "ethereum" });
```

Supply the correct chain for every endpoint. Setting only `probeIntervalSeconds: 0` still permits `eth_chainId` detection. With explicit chains and probes off, request and latency metrics remain available, and the wrapper measures head freshness from successful `eth_blockNumber` or `eth_getBlockByNumber("latest", …)` responses matched by ID. Passive head observations expire after 120 seconds. Scraping metrics never makes an RPC call. Call `telemetry.close()` during shutdown to clear its timers. Your app remains responsible for cancelling its requests and stopping its HTTP server.

With defaults, each discovered endpoint receives up to three chain-detection calls and an immediate head probe, followed by a head probe every 15 seconds. Options other than `chain` apply to the entire registry, so configure them consistently across wrappers. A provider label is the endpoint host, including a non-default port. Distinct paths on the same host and chain share metric labels.

## Add it with a coding agent

Use the [integration prompt](https://github.com/SolidRPC/rpc-xray#add-it-with-a-coding-agent). It covers package availability, request overhead, registry ownership and scrape verification.

## License

Apache-2.0
