import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { startApp } from "./server.mjs";

test("plain Node serves metrics without extra RPCs and shuts down polling", async (t) => {
  const calls = [];
  const upstream = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const call = JSON.parse(Buffer.concat(chunks));
    calls.push({ call, at: Date.now() });
    response.setHeader("content-type", "application/json");
    response.end(
      JSON.stringify({ jsonrpc: "2.0", id: call.id, result: "0x1234" }),
    );
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(() => {
    upstream.closeAllConnections();
    upstream.close();
  });
  const app = await startApp({
    rpcUrl: `http://127.0.0.1:${upstream.address().port}/secret-path?key=secret-query`,
    chain: "ethereum",
    port: 0,
  });
  t.after(() => app.close());
  const url = `http://127.0.0.1:${app.port}`;
  let metrics;
  for (let attempt = 0; attempt < 50; attempt++) {
    const response = await fetch(`${url}/metrics`);
    assert.match(
      response.headers.get("content-type"),
      /text\/plain; version=0.0.4/,
    );
    metrics = await response.text();
    if (metrics.includes('outcome="ok"')) break;
    await delay(20);
  }
  assert.match(
    metrics,
    /rpc_xray_requests_total\{[^\n]*chain="ethereum"[^\n]*outcome="ok"[^\n]*\} 1/,
  );
  assert.doesNotMatch(
    metrics,
    /secret-path|secret-query|rpc_xray_head_block|rpc_xray_probe_requests_total/,
  );
  assert.equal(calls.length, 1);
  assert.equal((await (await fetch(url)).json()).blockNumber, "0x1234");
  assert.equal((await fetch(`${url}/other`)).status, 404);
  await delay(1150);
  assert.equal(calls.length, 2);
  assert.ok(calls[1].at - calls[0].at >= 900);
  for (const { call } of calls) {
    assert.equal(call.method, "eth_blockNumber");
    assert.deepEqual(call.params, []);
  }
  await app.close();
  await delay(1100);
  assert.equal(calls.length, 2);
});

test("configuration errors do not disclose endpoint credentials", async () => {
  await assert.rejects(
    startApp({ rpcUrl: "secret-token", chain: "ethereum" }),
    { message: "Set RPC_URL to an HTTP or HTTPS JSON-RPC endpoint" },
  );
  await assert.rejects(startApp({ rpcUrl: "https://rpc.example/secret" }), {
    message: "Set CHAIN explicitly to avoid eth_chainId detection calls",
  });
});

test("pending RPCs skip ticks and shutdown aborts them", async (t) => {
  let count = 0;
  let received;
  const firstCall = new Promise((resolve) => {
    received = resolve;
  });
  const upstream = createServer((request) => {
    request.resume();
    count++;
    received();
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  t.after(() => {
    upstream.closeAllConnections();
    upstream.close();
  });
  const app = await startApp({
    rpcUrl: `http://127.0.0.1:${upstream.address().port}`,
    chain: "base",
    port: 0,
  });
  t.after(() => app.close());
  await firstCall;
  await delay(1100);
  assert.equal(count, 1);
  await app.close();
  await delay(1100);
  assert.equal(count, 1);
});
