import { Xray, type FetchLike, type WrapOptions, type XrayOptions } from "./xray.js";

export { Xray, VERSION } from "./xray.js";
export type { FetchLike, WrapOptions, XrayOptions } from "./xray.js";
export type { Outcome } from "./classify.js";

const CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8";

const shared = new Xray();

export function xray<T extends FetchLike>(fetchImpl: T, options?: WrapOptions): T {
  return shared.wrap(fetchImpl, options);
}

export function configure(options: XrayOptions): void {
  shared.configure(options);
}

export function metrics(): string {
  return shared.metrics();
}

export function metricsResponse(): Response {
  return new Response(shared.metrics(), { headers: { "content-type": CONTENT_TYPE } });
}

interface NodeResponse {
  setHeader(name: string, value: string): unknown;
  end(body: string): unknown;
}

export function metricsHandler(): (request: unknown, response: NodeResponse) => void {
  return (_request, response) => {
    response.setHeader("content-type", CONTENT_TYPE);
    response.end(shared.metrics());
  };
}

export async function serveMetrics(options?: { port?: number; host?: string }): Promise<void> {
  try {
    const http = await import("node:http");
    const server = http.createServer(metricsHandler());
    server.on("error", () => undefined);
    server.listen(options?.port ?? 9464, options?.host ?? "0.0.0.0");
    server.unref();
  } catch {
    return;
  }
}

interface EthersRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: Uint8Array | null;
}

interface EthersSignal {
  addListener(listener: () => void): void;
}

interface EthersResponse {
  statusCode: number;
  statusMessage: string;
  headers: Record<string, string>;
  body: Uint8Array | null;
}

export function xrayEthers(
  options?: WrapOptions,
): (request: EthersRequest, signal?: EthersSignal) => Promise<EthersResponse> {
  const wrapped = shared.wrap(
    (input: string, init?: RequestInit) => globalThis.fetch(input, init),
    options,
  );
  return async (request, signal) => {
    const controller = new AbortController();
    signal?.addListener(() => controller.abort());
    const init: RequestInit = {
      method: request.method,
      headers: request.headers,
      signal: controller.signal,
    };
    if (request.body) init.body = request.body as unknown as BodyInit;
    const response = await wrapped(request.url, init);
    const headers: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      headers[key.toLowerCase()] = value;
    });
    const buffer = await response.arrayBuffer();
    return {
      statusCode: response.status,
      statusMessage: response.statusText,
      headers,
      body: buffer.byteLength > 0 ? new Uint8Array(buffer) : null,
    };
  };
}
