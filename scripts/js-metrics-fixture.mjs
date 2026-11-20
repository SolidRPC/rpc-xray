import { writeFile } from "node:fs/promises";
import { Xray } from "../js/dist/index.js";

const telemetry = new Xray({ offers: false, valuePerRequest: 0.1, revenuePerHour: 100 });
const fetchImpl = async (_url, init) => {
  const request = JSON.parse(init.body);
  if (request.method === "eth_call") await new Promise((resolve) => setTimeout(resolve, 1100));
  return new Response(JSON.stringify({ jsonrpc: "2.0", id: request.id, result: "0x10" }));
};
try {
  const wrapped = telemetry.wrap(fetchImpl, { chain: "ethereum" });
  await (await wrapped("https://rpc.example", {
    method: "POST",
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_call", params: [] }),
  })).text();
  for (let i = 0; i < 100 && !telemetry.metrics().includes("rpc_xray_slow_seconds_total"); i++) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  await writeFile(process.argv[2], telemetry.metrics());
} finally {
  telemetry.close();
}
