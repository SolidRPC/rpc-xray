import { createServer } from "node:http";
import { pathToFileURL } from "node:url";
import { Xray } from "@solidrpc/rpc-xray";

export async function startApp({
  rpcUrl,
  chain,
  port = 9464,
  host = "127.0.0.1",
  intervalMs = 1000,
}) {
  let endpoint;
  try {
    endpoint = new URL(rpcUrl);
  } catch {
    throw new Error("Set RPC_URL to an HTTP or HTTPS JSON-RPC endpoint");
  }
  if (!["http:", "https:"].includes(endpoint.protocol)) {
    throw new Error("RPC_URL must use HTTP or HTTPS");
  }
  if (!chain?.trim())
    throw new Error(
      "Set CHAIN explicitly to avoid eth_chainId detection calls",
    );
  if (!Number.isInteger(port) || port < 0 || port > 65535)
    throw new Error("PORT must be a valid port number");
  if (!Number.isFinite(intervalMs) || intervalMs < 1000)
    throw new Error("Polling interval must be at least 1000 ms");

  const telemetry = new Xray({ probeIntervalSeconds: 0, offers: false });
  const rpcFetch = telemetry.wrap(fetch, { chain });
  const controller = new AbortController();
  let inFlight;
  let stopped = false;
  let id = 0;
  let state = { status: "starting", blockNumber: null, updatedAt: null };

  function poll() {
    if (stopped || inFlight) return;
    inFlight = (async () => {
      const request = new AbortController();
      const timeout = setTimeout(() => request.abort(), 4500);
      const abort = () => request.abort();
      controller.signal.addEventListener("abort", abort, { once: true });
      try {
        const response = await rpcFetch(rpcUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            jsonrpc: "2.0",
            id: ++id,
            method: "eth_blockNumber",
            params: [],
          }),
          signal: request.signal,
        });
        const body = await response.json();
        if (!response.ok || body.error || !/^0x[0-9a-f]+$/i.test(body.result)) {
          throw new Error("RPC request failed");
        }
        state = {
          status: "ok",
          blockNumber: body.result,
          updatedAt: new Date().toISOString(),
        };
      } catch {
        state = { ...state, status: "error" };
      } finally {
        clearTimeout(timeout);
        controller.signal.removeEventListener("abort", abort);
      }
    })().finally(() => {
      inFlight = undefined;
    });
  }

  const server = createServer((request, response) => {
    if (request.method === "GET" && request.url === "/metrics") {
      response.writeHead(200, {
        "content-type": "text/plain; version=0.0.4; charset=utf-8",
      });
      response.end(telemetry.metrics());
    } else if (request.method === "GET" && request.url === "/") {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ chain, ...state }));
    } else {
      response.writeHead(404);
      response.end("Not found");
    }
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, resolve);
  });
  const timer = setInterval(poll, intervalMs);
  poll();

  return {
    port: server.address().port,
    async close() {
      stopped = true;
      clearInterval(timer);
      controller.abort();
      telemetry.close();
      server.closeAllConnections();
      await Promise.all([
        new Promise((resolve) => server.close(resolve)),
        inFlight,
      ]);
    },
  };
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  try {
    const app = await startApp({
      rpcUrl: process.env.RPC_URL,
      chain: process.env.CHAIN,
      host: process.env.HOST || "127.0.0.1",
      port: Number(process.env.PORT || 9464),
    });
    console.log(
      `RPC example listening on port ${app.port}. Metrics at /metrics. Press Ctrl+C to stop.`,
    );
    for (const signal of ["SIGINT", "SIGTERM"])
      process.once(signal, () => {
        void app.close();
      });
  } catch (error) {
    console.error(
      error.code ? `Server could not start (${error.code})` : error.message,
    );
    process.exitCode = 1;
  }
}
