# @solidrpc/rpc-xray

**See every request your RPC provider failed.**

A pass-through wrapper for JavaScript and TypeScript. It measures your real EVM JSON-RPC traffic and exposes Prometheus metrics. Dashboards, alert rules and Slack incident messages are in the [main repository](https://github.com/SolidRPC/rpc-xray).

Node.js 20 or newer. No runtime dependencies.

## Install

```bash
npm install @solidrpc/rpc-xray
```

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

Pick one:

```ts
await serveMetrics(); // http://0.0.0.0:9464/metrics
app.get("/metrics", metricsHandler()); // Express, Fastify (raw), node:http
export const GET = () => metricsResponse(); // Next.js, Hono, any fetch-style router
const text = metrics(); // append to your own exposition
```

## Options

Pass options as the second argument: `xray(fetch, { chain: "base", offers: false })`. The full list is in the [main README](https://github.com/SolidRPC/rpc-xray#configuration).

## Add it with a coding agent

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

## License

Apache-2.0
