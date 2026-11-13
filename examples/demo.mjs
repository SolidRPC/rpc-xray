import { createServer } from "node:http";
import { serveMetrics, xray } from "../js/dist/index.js";

const CYCLE_SECONDS = 1200;
const started = Date.now();

function phase() {
  const second = ((Date.now() - started) / 1000) % CYCLE_SECONDS;
  if (second >= 90 && second < 330) return "errors";
  if (second >= 420 && second < 720) return "stale";
  if (second >= 780 && second < 1080) return "slow";
  return "normal";
}

function provider(port, flaky) {
  let head = 21_000_000;
  setInterval(() => {
    const tip = 21_000_000 + Math.floor((Date.now() - started) / 2000);
    if (!(flaky && phase() === "stale")) head = tip;
  }, 500).unref();
  createServer((request, response) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const call = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      const now = phase();
      const roll = Math.random();
      const reply = (status, body, delay = 5 + Math.random() * 40) =>
        setTimeout(() => {
          response.statusCode = status;
          response.setHeader("content-type", "application/json");
          response.end(JSON.stringify(body));
        }, delay);
      const result = (value) => ({ jsonrpc: "2.0", id: call.id, result: value });
      const error = (code, message) => ({ jsonrpc: "2.0", id: call.id, error: { code, message } });
      if (call.method === "eth_chainId") return reply(200, result("0x1"));
      if (call.method === "eth_blockNumber") return reply(200, result(`0x${head.toString(16)}`));
      if (flaky) {
        const errors = now === "errors" ? 0.25 : 0.012;
        if (roll < errors * 0.4) return reply(503, { message: "upstream unavailable" });
        if (roll < errors * 0.7) return reply(429, { message: "too many requests" });
        if (roll < errors) return reply(200, error(-32000, "header not found"));
        if (now === "slow" || roll > 0.97) return reply(200, result("0x0"), 1100 + Math.random() * 1500);
      } else if (roll < 0.0005) {
        return reply(502, { message: "bad gateway" });
      }
      if (call.method === "eth_call" && roll > 0.9) {
        return reply(200, error(3, "execution reverted"));
      }
      return reply(200, result("0x0"));
    });
  }).listen(port, "127.0.0.1");
  return `http://127.0.0.1:${port}/v1/demo-key`;
}

const steady = provider(8551, false);
const flaky = provider(8552, true);

const fetchWithXray = xray(fetch, { valuePerRequest: 0.002 });
const methods = ["eth_call", "eth_getBalance", "eth_getLogs", "eth_getTransactionReceipt", "eth_call"];

function send(url) {
  const method = methods[Math.floor(Math.random() * methods.length)];
  fetchWithXray(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: [] }),
    signal: AbortSignal.timeout(5000),
  })
    .then((response) => response.text())
    .catch(() => undefined);
}

setInterval(() => {
  for (let index = 0; index < 4; index += 1) send(flaky);
  send(steady);
}, 100);

await serveMetrics({ port: 9464 });
console.log("rpc-xray demo: metrics on :9464, phases repeat every 20 minutes");
