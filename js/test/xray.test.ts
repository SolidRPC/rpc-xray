import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { FetchRequest, JsonRpcProvider } from "ethers";
import { createPublicClient, http } from "viem";
import { classifyFailure, classifyResponse, parseRequest } from "../src/classify.js";
import { Xray, xray, xrayEthers, metrics, type FetchLike } from "../src/index.js";

interface Vector {
  name: string;
  request: string;
  status?: number;
  response?: string | null;
  failure?: "timeout" | "network";
  expected: { method: string; outcome: string }[];
}

const vectors = JSON.parse(
  readFileSync(new URL("../../spec/vectors.json", import.meta.url), "utf8"),
) as Vector[];

interface Mock {
  server: Server;
  url: string;
  head: number;
  chainId: number;
}

async function startMock(chainId: number, head: number): Promise<Mock> {
  const mock: Mock = { server: createServer(), url: "", head, chainId };
  mock.server.on("request", (request, response) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      const path = request.url ?? "/";
      const text = Buffer.concat(chunks).toString("utf8");
      if (path.startsWith("/status/")) {
        response.statusCode = Number(path.slice(8));
        response.end("nope");
        return;
      }
      if (path === "/big") {
        response.setHeader("content-type", "application/json");
        response.end(JSON.stringify({ jsonrpc: "2.0", id: 1, result: "x".repeat(4096) }));
        return;
      }
      if (path === "/slow") {
        setTimeout(() => response.end('{"jsonrpc":"2.0","id":1,"result":"0x1"}'), 1100);
        return;
      }
      const answer = (call: { id: unknown; method: string }) => {
        if (call.method === "eth_chainId") {
          return { jsonrpc: "2.0", id: call.id, result: `0x${mock.chainId.toString(16)}` };
        }
        if (call.method === "eth_blockNumber") {
          return { jsonrpc: "2.0", id: call.id, result: `0x${mock.head.toString(16)}` };
        }
        if (call.method === "eth_call") {
          return { jsonrpc: "2.0", id: call.id, error: { code: 3, message: "execution reverted" } };
        }
        if (call.method === "eth_getBalance") {
          return {
            jsonrpc: "2.0",
            id: call.id,
            error: { code: -32000, message: "header not found" },
          };
        }
        return { jsonrpc: "2.0", id: call.id, result: "0x0" };
      };
      const parsed = JSON.parse(text) as
        { id: unknown; method: string } | { id: unknown; method: string }[];
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify(Array.isArray(parsed) ? parsed.map(answer) : answer(parsed)));
    });
  });
  await new Promise<void>((resolve) => mock.server.listen(0, "127.0.0.1", resolve));
  mock.url = `http://127.0.0.1:${(mock.server.address() as AddressInfo).port}`;
  return mock;
}

function rpc(method: string, id = 1): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id, method, params: [] }),
  };
}

async function until(read: () => string, pattern: RegExp): Promise<string> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const text = read();
    if (pattern.test(text)) return text;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.fail(`metrics never matched ${String(pattern)}:\n${read()}`);
}

let mock: Mock;
let lagging: Mock;

before(async () => {
  mock = await startMock(1, 100);
  lagging = await startMock(1, 90);
});

after(() => {
  mock.server.close();
  lagging.server.close();
  mock.server.closeAllConnections();
  lagging.server.closeAllConnections();
});

for (const vector of vectors) {
  test(`vector: ${vector.name}`, () => {
    const request = parseRequest(vector.request);
    if (!request) {
      assert.deepEqual(vector.expected, []);
      return;
    }
    const entries = vector.failure
      ? classifyFailure(request, vector.failure)
      : classifyResponse(request, vector.status ?? 200, vector.response ?? undefined);
    assert.deepEqual(entries, vector.expected);
  });
}

test("returns the same promise and an untouched body", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  let inner: Promise<Response> | undefined;
  const base = (input: string, init?: RequestInit) => {
    inner = fetch(input, init);
    return inner;
  };
  const wrapped = instance.wrap(base);
  const promise = wrapped(mock.url, rpc("eth_gasPrice"));
  assert.equal(promise, inner);
  const response = await promise;
  assert.equal(await response.text(), '{"jsonrpc":"2.0","id":1,"result":"0x0"}');
  const text = await until(() => instance.metrics(), /rpc_xray_requests_total\{/);
  assert.match(
    text,
    /rpc_xray_requests_total\{chain="ethereum",provider="127\.0\.0\.1:\d+",method="eth_gasPrice",outcome="ok"\} 1/,
  );
  assert.match(text, /rpc_xray_request_duration_seconds_count\{[^}]*method="eth_gasPrice"\} 1/);
  instance.close();
});

test("classifies live outcomes and batches", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  const wrapped = instance.wrap(fetch, { chain: "base" });
  await (await wrapped(mock.url, rpc("eth_call"))).text();
  await (await wrapped(mock.url, rpc("eth_getBalance"))).text();
  await (await wrapped(`${mock.url}/status/429`, rpc("eth_getLogs"))).text();
  await (await wrapped(`${mock.url}/status/502`, rpc("eth_getLogs"))).text();
  const batch = [
    { jsonrpc: "2.0", id: 1, method: "eth_blockNumber" },
    { jsonrpc: "2.0", id: 2, method: "eth_call" },
  ];
  await (await wrapped(mock.url, { method: "POST", body: JSON.stringify(batch) })).json();
  const text = await until(() => instance.metrics(), /method="batch"/);
  assert.match(text, /chain="base",[^}]*method="eth_call",outcome="caller_error"\} 2/);
  assert.match(text, /method="eth_getBalance",outcome="rpc_error"\} 1/);
  assert.match(text, /method="eth_getLogs",outcome="rate_limited"\} 1/);
  assert.match(text, /method="eth_getLogs",outcome="http_error"\} 1/);
  assert.match(text, /method="eth_blockNumber",outcome="ok"\} 1/);
  instance.close();
});

test("a rejected request reaches the caller unchanged", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  const failure = new TypeError("fetch failed");
  const wrapped = instance.wrap<FetchLike>(() => Promise.reject(failure), { chain: "ethereum" });
  await assert.rejects(
    wrapped("http://rpc.invalid/", rpc("eth_call")),
    (error) => error === failure,
  );
  const text = await until(() => instance.metrics(), /outcome="network"/);
  assert.match(text, /provider="rpc\.invalid",method="eth_call",outcome="network"\} 1/);
  const timeout = Object.assign(new Error("aborted"), { name: "AbortError" });
  const aborted = instance.wrap<FetchLike>(() => Promise.reject(timeout), { chain: "ethereum" });
  await assert.rejects(aborted("http://rpc.invalid/", rpc("eth_call")));
  await until(() => instance.metrics(), /outcome="timeout"\} 1/);
  instance.close();
});

test("internal failures never reach the caller", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  const broken = {
    status: 200,
    get headers(): Headers {
      throw new Error("boom");
    },
    clone(): Response {
      throw new Error("boom");
    },
  } as unknown as Response;
  const wrapped = instance.wrap<FetchLike>(() => Promise.resolve(broken), { chain: "ethereum" });
  assert.equal(await wrapped("http://rpc.invalid/", rpc("eth_call")), broken);
  assert.equal(await wrapped("not a url", rpc("eth_call")), broken);
  assert.equal(await wrapped("http://rpc.invalid/", { method: "POST", body: "{" }), broken);
  const sync = instance.wrap<FetchLike>(() => {
    throw new RangeError("sync");
  });
  assert.throws(() => sync("http://rpc.invalid/", rpc("eth_call")), RangeError);
  instance.close();
});

test("non JSON-RPC traffic is not recorded", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  const wrapped = instance.wrap(fetch, { chain: "ethereum" });
  await (await wrapped(`${mock.url}/status/200`)).text();
  await (await wrapped(`${mock.url}/status/200`, { method: "POST", body: '{"query":"x"}' })).text();
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.doesNotMatch(instance.metrics(), /rpc_xray_requests_total\{/);
  instance.close();
});

test("an oversized body is delivered whole and classified by status", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0, maxBodyBytes: 512 });
  const wrapped = instance.wrap(fetch, { chain: "ethereum" });
  const response = await wrapped(`${mock.url}/big`, rpc("eth_getLogs"));
  const payload = (await response.json()) as { result: string };
  assert.equal(payload.result.length, 4096);
  await until(() => instance.metrics(), /method="eth_getLogs",outcome="ok"\} 1/);
  instance.close();
});

test("slow requests add to the slow seconds counter", async () => {
  const instance = new Xray({ probeIntervalSeconds: 0 });
  const wrapped = instance.wrap(fetch, { chain: "ethereum" });
  await (await wrapped(`${mock.url}/slow`, rpc("eth_call"))).text();
  const text = await until(() => instance.metrics(), /rpc_xray_slow_seconds_total\{/);
  const value = Number(/rpc_xray_slow_seconds_total\{[^}]*\} (\S+)/.exec(text)?.[1]);
  assert.ok(value > 0 && value < 1, `slow seconds ${value}`);
  instance.close();
});

test("the head probe reports head, age and lag", async () => {
  const instance = new Xray({ probeIntervalSeconds: 1 });
  const wrapped = instance.wrap(fetch);
  await (await wrapped(mock.url, rpc("eth_gasPrice"))).text();
  await (await wrapped(lagging.url, rpc("eth_gasPrice"))).text();
  const text = await until(() => instance.metrics(), /rpc_xray_head_lag_blocks\{[^}]*\} 10/);
  assert.match(text, /rpc_xray_head_block\{chain="ethereum",[^}]*\} 100/);
  assert.match(text, /rpc_xray_head_block\{chain="ethereum",[^}]*\} 90/);
  assert.match(text, /rpc_xray_head_lag_blocks\{[^}]*\} 0/);
  assert.match(text, /rpc_xray_probe_requests_total\{[^}]*result="ok"\} \d+/);
  assert.match(text, /rpc_xray_head_age_seconds\{/);
  assert.doesNotMatch(text, /method="eth_blockNumber"/);
  mock.head = 85;
  try {
    await until(() => instance.metrics(), /rpc_xray_head_lag_blocks\{[^}]*\} 5\n/);
  } finally {
    mock.head = 100;
    instance.close();
  }
});

test("works as a viem transport", async () => {
  const client = createPublicClient({
    transport: http(mock.url, { fetchFn: xray(fetch, { probeIntervalSeconds: 0 }) }),
  });
  assert.equal(await client.getBlockNumber(), 100n);
  await until(metrics, /method="eth_blockNumber",outcome="ok"\} 1/);
});

test("works as an ethers transport", async () => {
  const request = new FetchRequest(mock.url);
  request.getUrlFunc = xrayEthers({ probeIntervalSeconds: 0 });
  const provider = new JsonRpcProvider(request, 1, { staticNetwork: true, batchMaxCount: 1 });
  assert.equal(await provider.getBlockNumber(), 100);
  await until(metrics, /method="eth_blockNumber",outcome="ok"\} 2/);
  provider.destroy();
});

test(
  "the exposition passes promtool",
  { skip: spawnSync("promtool", ["--version"]).error !== undefined },
  async () => {
    const instance = new Xray({ probeIntervalSeconds: 1, valuePerRequest: 0.002 });
    const wrapped = instance.wrap(fetch);
    await (await wrapped(mock.url, rpc("eth_call"))).text();
    const text = await until(() => instance.metrics(), /rpc_xray_head_block\{/);
    execFileSync("promtool", ["check", "metrics"], {
      input: text,
      stdio: ["pipe", "pipe", "pipe"],
    });
    instance.close();
  },
);

test("passive head uses only a matching latest block without extra calls", async (t) => {
  const telemetry = new Xray({ probeIntervalSeconds: 0 });
  let calls = 0;
  const send = telemetry.wrap(
    async (_url: string, _init?: RequestInit) => {
      calls++;
      return new Response(
        JSON.stringify([
          { id: 2, result: { number: "0xffff" } },
          { id: 1, result: { number: "0x29" } },
          { id: 3, result: { number: "wrong" } },
          { id: 9, result: { number: "0xff" } },
        ]),
      );
    },
    { chain: "ethereum" },
  );
  try {
    const body = JSON.stringify(
      ["latest", "safe", "latest"].map((tag, i) => ({
        method: "eth_getBlockByNumber",
        params: [tag, false],
        id: i + 1,
      })),
    );
    await send("http://rpc.example", { method: "POST", body });
    const text = await until(() => telemetry.metrics(), /rpc_xray_head_block\{[^}]+\} 41/);
    const expired = Date.now() + 121_000;
    t.mock.method(Date, "now", () => expired);
    assert.doesNotMatch(telemetry.metrics(), /rpc_xray_head_block\{/);
    assert.equal(calls, 1);
    assert.doesNotMatch(text, /rpc_xray_probe_requests_total\{/);
  } finally {
    telemetry.close();
  }
});
