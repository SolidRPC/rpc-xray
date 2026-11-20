import { chainLabel } from "./chains.js";
import {
  classifyFailure,
  classifyResponse,
  parseRequest,
  type Entry,
  type ParsedRequest,
} from "./classify.js";
import { Registry, type GaugeSample } from "./registry.js";

export const VERSION = "0.1.2";

const MAX_ENDPOINTS = 32;
const MAX_PENDING = 2000;
const PROBE_TIMEOUT_MS = 10_000;
const DETECT_ATTEMPTS = 3;
const STALE_SECONDS = 180;
const OFFER_MONTHLY_REQUESTS = 100_000_000;
const OFFER_MIN_UPTIME_SECONDS = 24 * 3600;
const OFFER_FAILURE_RATIO = 0.01;
const FAILURES = new Set(["http_error", "rate_limited", "rpc_error", "timeout", "network"]);

export type FetchLike = (input: any, init?: any) => Promise<Response>;

export interface XrayOptions {
  maxBodyBytes?: number;
  probeIntervalSeconds?: number;
  valuePerRequest?: number;
  revenuePerHour?: number;
  offers?: boolean;
  logger?: (line: string) => void;
}

export interface WrapOptions extends XrayOptions {
  chain?: string;
}

interface Pending {
  entries: Entry[];
  seconds: number;
  batch: boolean;
}

interface Endpoint {
  url: string;
  provider: string;
  chain: string | undefined;
  pending: Pending[];
  head: number | undefined;
  lastAdvance: number | undefined;
  headers: unknown;
  fetchImpl: FetchLike;
  attempts: number;
  timer: ReturnType<typeof setInterval> | undefined;
  busy: boolean;
}

interface Context {
  endpoint: Endpoint;
  request: ParsedRequest;
}

function nowMs(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function unref(timer: unknown): void {
  const candidate = timer as { unref?: () => void };
  if (typeof candidate.unref === "function") candidate.unref();
}

function bodyText(body: unknown, cap: number): string | undefined {
  if (typeof body === "string") return body.length <= cap ? body : undefined;
  if (body instanceof ArrayBuffer) {
    return body.byteLength <= cap ? new TextDecoder().decode(body) : undefined;
  }
  if (ArrayBuffer.isView(body)) {
    return body.byteLength <= cap ? new TextDecoder().decode(body) : undefined;
  }
  return undefined;
}

async function readCapped(copy: Response, cap: number): Promise<string | undefined> {
  const stream = copy.body as ReadableStream<Uint8Array> | null;
  if (!stream) return "";
  if (typeof stream.getReader !== "function") {
    const length = Number(copy.headers.get("content-length"));
    return Number.isFinite(length) && length <= cap ? await copy.text() : undefined;
  }
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let text = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > cap) {
      void reader.cancel().catch(() => undefined);
      return undefined;
    }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}

export class Xray {
  private readonly registry = new Registry();
  private readonly endpoints = new Map<string, Endpoint>();
  private readonly startedAt = Date.now();
  private overflow: Endpoint | undefined;
  private options: Required<Omit<XrayOptions, "valuePerRequest" | "revenuePerHour" | "logger">> &
    Pick<XrayOptions, "valuePerRequest" | "revenuePerHour" | "logger"> = {
    maxBodyBytes: 1024 * 1024,
    probeIntervalSeconds: 15,
    offers: true,
  };
  private total = 0;
  private failed = 0;
  private staleSeen = false;
  private offered = false;

  constructor(options?: XrayOptions) {
    this.configure(options);
  }

  configure(options?: XrayOptions): void {
    if (!options) return;
    for (const key of [
      "maxBodyBytes",
      "probeIntervalSeconds",
      "valuePerRequest",
      "revenuePerHour",
      "offers",
      "logger",
    ] as const) {
      if (options[key] !== undefined) (this.options as Record<string, unknown>)[key] = options[key];
    }
  }

  wrap<T extends FetchLike>(fetchImpl: T, options?: WrapOptions): T {
    try {
      this.configure(options);
    } catch {
      return fetchImpl;
    }
    const chain = options?.chain;
    const wrapped = (input: unknown, init?: unknown): Promise<Response> => {
      let context: Context | undefined;
      let started = 0;
      try {
        started = nowMs();
        context = this.begin(fetchImpl, input, init, chain);
      } catch {
        context = undefined;
      }
      const promise = fetchImpl(input, init);
      if (!context) return promise;
      const active = context;
      try {
        promise.then(
          (response) => {
            try {
              this.onResponse(active, response, started);
            } catch {
              return;
            }
          },
          (error: unknown) => {
            try {
              this.onFailure(active, error, started);
            } catch {
              return;
            }
          },
        );
      } catch {
        return promise;
      }
      return promise;
    };
    return wrapped as T;
  }

  metrics(): string {
    try {
      return this.registry.render(this.gauges());
    } catch {
      return "";
    }
  }

  close(): void {
    for (const endpoint of this.endpoints.values()) {
      if (endpoint.timer) clearInterval(endpoint.timer);
      endpoint.timer = undefined;
    }
  }

  private begin(
    fetchImpl: FetchLike,
    input: unknown,
    init: unknown,
    chain: string | undefined,
  ): Context | undefined {
    const options = (init ?? {}) as { method?: string; body?: unknown; headers?: unknown };
    const request = input as { url?: unknown; method?: unknown };
    const isRequest = typeof input === "object" && input !== null && !(input instanceof URL);
    const method = options.method ?? (isRequest ? request.method : undefined);
    if (typeof method !== "string" || method.toUpperCase() !== "POST") return undefined;
    const text = bodyText(options.body, this.options.maxBodyBytes);
    if (text === undefined) return undefined;
    const parsed = parseRequest(text);
    if (!parsed) return undefined;
    const raw = isRequest ? request.url : input;
    const url = new URL(String(raw));
    return { endpoint: this.endpoint(url, fetchImpl, options.headers, chain), request: parsed };
  }

  private endpoint(
    url: URL,
    fetchImpl: FetchLike,
    headers: unknown,
    chain: string | undefined,
  ): Endpoint {
    const key = url.href;
    const existing = this.endpoints.get(key);
    if (existing) return existing;
    if (this.endpoints.size >= MAX_ENDPOINTS) {
      this.overflow ??= {
        url: "",
        provider: "other",
        chain: chain ?? "unknown",
        pending: [],
        head: undefined,
        lastAdvance: undefined,
        headers: undefined,
        fetchImpl,
        attempts: 0,
        timer: undefined,
        busy: false,
      };
      return this.overflow;
    }
    const endpoint: Endpoint = {
      url: key,
      provider: url.host.toLowerCase(),
      chain,
      pending: [],
      head: undefined,
      lastAdvance: undefined,
      headers,
      fetchImpl,
      attempts: 0,
      timer: undefined,
      busy: false,
    };
    this.endpoints.set(key, endpoint);
    this.schedule(endpoint);
    return endpoint;
  }

  private schedule(endpoint: Endpoint): void {
    const interval = this.options.probeIntervalSeconds;
    const run = () => {
      void this.tick(endpoint).catch(() => undefined);
    };
    unref(setTimeout(run, 0));
    const period = interval > 0 ? interval * 1000 : 5000;
    endpoint.timer = setInterval(() => {
      if (interval <= 0 && endpoint.chain !== undefined) {
        if (endpoint.timer) clearInterval(endpoint.timer);
        endpoint.timer = undefined;
        return;
      }
      run();
    }, period);
    unref(endpoint.timer);
  }

  private async call(endpoint: Endpoint, method: string): Promise<unknown> {
    const headers = new Headers((endpoint.headers ?? undefined) as HeadersInit | undefined);
    headers.set("content-type", "application/json");
    headers.delete("content-length");
    const response = await endpoint.fetchImpl(endpoint.url, {
      method: "POST",
      headers,
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params: [] }),
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    if (!response.ok) throw new Error("probe failed");
    const payload = (await response.json()) as { id?: unknown; result?: unknown; error?: unknown };
    if (
      payload.id !== 1 ||
      payload.error != null ||
      typeof payload.result !== "string" ||
      !/^0x[0-9a-f]{1,16}$/i.test(payload.result)
    )
      throw new Error("invalid probe response");
    const result = Number(payload.result);
    if (!Number.isSafeInteger(result)) throw new Error("probe result exceeds safe integer range");
    return result;
  }

  private async tick(endpoint: Endpoint): Promise<void> {
    if (endpoint.busy) return;
    endpoint.busy = true;
    try {
      if (endpoint.chain === undefined) await this.detect(endpoint);
      if (this.options.probeIntervalSeconds > 0) await this.probe(endpoint);
    } finally {
      endpoint.busy = false;
    }
  }

  private async detect(endpoint: Endpoint): Promise<void> {
    endpoint.attempts += 1;
    try {
      const id = (await this.call(endpoint, "eth_chainId")) as number;
      if (!Number.isFinite(id)) throw new Error("bad chain id");
      endpoint.chain = chainLabel(id);
    } catch {
      if (endpoint.attempts < DETECT_ATTEMPTS) return;
      endpoint.chain = "unknown";
    }
    const pending = endpoint.pending;
    endpoint.pending = [];
    for (const item of pending) this.write(endpoint, endpoint.chain, item);
  }

  private async probe(endpoint: Endpoint): Promise<void> {
    const chain = endpoint.chain ?? "unknown";
    const labels = { chain, provider: endpoint.provider };
    try {
      const head = (await this.call(endpoint, "eth_blockNumber")) as number;
      if (!Number.isFinite(head)) throw new Error("bad head");
      if (endpoint.head === undefined || head > endpoint.head) {
        endpoint.lastAdvance = Date.now();
      }
      endpoint.head = head;
      this.registry.inc("rpc_xray_probe_requests_total", "Head probe calls by result.", {
        ...labels,
        result: "ok",
      });
    } catch {
      this.registry.inc("rpc_xray_probe_requests_total", "Head probe calls by result.", {
        ...labels,
        result: "error",
      });
    }
    if (
      endpoint.lastAdvance !== undefined &&
      Date.now() - endpoint.lastAdvance > STALE_SECONDS * 1000
    ) {
      this.staleSeen = true;
    }
  }

  private onFailure(context: Context, error: unknown, started: number): void {
    const name = (error as { name?: unknown } | null)?.name;
    const kind = name === "AbortError" || name === "TimeoutError" ? "timeout" : "network";
    this.finish(context, classifyFailure(context.request, kind), started);
  }

  private onResponse(context: Context, response: Response, started: number): void {
    const status = response.status;
    if (status >= 400) {
      this.finish(context, classifyResponse(context.request, status, undefined), started);
      return;
    }
    const cap = this.options.maxBodyBytes;
    const length = Number(response.headers.get("content-length") ?? Number.NaN);
    if (Number.isFinite(length) && length > cap) {
      this.finish(context, classifyResponse(context.request, status, undefined), started);
      return;
    }
    const copy = response.clone();
    readCapped(copy, cap).then(
      (text) => {
        try {
          this.finish(context, classifyResponse(context.request, status, text), started);
        } catch {
          return;
        }
      },
      () => {
        try {
          this.finish(context, classifyFailure(context.request, "network"), started);
        } catch {
          return;
        }
      },
    );
  }

  private finish(context: Context, entries: Entry[], started: number): void {
    const item: Pending = {
      entries,
      seconds: Math.max(0, (nowMs() - started) / 1000),
      batch: context.request.batch,
    };
    const endpoint = context.endpoint;
    if (endpoint.chain !== undefined) {
      this.write(endpoint, endpoint.chain, item);
    } else if (endpoint.pending.length < MAX_PENDING) {
      endpoint.pending.push(item);
    } else {
      this.write(endpoint, "unknown", item);
    }
  }

  private write(endpoint: Endpoint, chain: string, item: Pending): void {
    const base = { chain, provider: endpoint.provider };
    for (const entry of item.entries) {
      this.registry.inc("rpc_xray_requests_total", "JSON-RPC calls by outcome.", {
        ...base,
        method: entry.method,
        outcome: entry.outcome,
      });
      this.total += 1;
      if (FAILURES.has(entry.outcome)) this.failed += 1;
    }
    const first = item.entries[0];
    if (!first) return;
    this.registry.observe(
      "rpc_xray_request_duration_seconds",
      "Duration of JSON-RPC HTTP requests.",
      { ...base, method: item.batch ? "batch" : first.method },
      item.seconds,
    );
    if (item.seconds > 1) {
      this.registry.inc(
        "rpc_xray_slow_seconds_total",
        "Seconds spent waiting beyond one second per request.",
        base,
        item.seconds - 1,
      );
    }
    if (this.total % 10_000 < item.entries.length) this.maybeOffer();
  }

  private maybeOffer(): void {
    if (this.offered || !this.options.offers) return;
    const uptime = (Date.now() - this.startedAt) / 1000;
    if (uptime < OFFER_MIN_UPTIME_SECONDS) return;
    const monthly = (this.total / uptime) * 30 * 24 * 3600;
    if (monthly < OFFER_MONTHLY_REQUESTS) return;
    if (this.failed / this.total < OFFER_FAILURE_RATIO && !this.staleSeen) return;
    this.offered = true;
    const line =
      `rpc-xray: this process sends about ${Math.round(monthly / 1_000_000)}M RPC requests a month ` +
      `and ${this.failed} of ${this.total} failed. SolidRPC offers a 14-day free trial or $100 credit. ` +
      `Email hello@solidrpc.io. Set offers: false to hide this message.`;
    (this.options.logger ?? console.info)(line);
  }

  private gauges(): GaugeSample[] {
    const now = Date.now();
    const best = new Map<string, number>();
    const known = [...this.endpoints.values()].filter(
      (endpoint) => endpoint.chain !== undefined && endpoint.head !== undefined,
    );
    for (const endpoint of known) {
      const chain = endpoint.chain as string;
      best.set(chain, Math.max(best.get(chain) ?? 0, endpoint.head as number));
    }
    const perEndpoint = (
      name: string,
      help: string,
      value: (endpoint: Endpoint) => number,
    ): GaugeSample[] =>
      known.map((endpoint) => ({
        name,
        help,
        labels: { chain: endpoint.chain as string, provider: endpoint.provider },
        value: value(endpoint),
      }));
    const samples: GaugeSample[] = [
      ...perEndpoint(
        "rpc_xray_head_block",
        "Latest block reported by the endpoint.",
        (e) => e.head as number,
      ),
      ...perEndpoint(
        "rpc_xray_head_last_advance_timestamp_seconds",
        "Unix time the head last advanced.",
        (e) => (e.lastAdvance as number) / 1000,
      ),
      ...perEndpoint("rpc_xray_head_age_seconds", "Seconds since the head last advanced.", (e) =>
        Math.max(0, (now - (e.lastAdvance as number)) / 1000),
      ),
      ...perEndpoint(
        "rpc_xray_head_lag_blocks",
        "Blocks behind the best endpoint of the same chain.",
        (e) => (best.get(e.chain as string) as number) - (e.head as number),
      ),
    ];
    if (this.options.valuePerRequest !== undefined) {
      samples.push({
        name: "rpc_xray_value_per_request_usd",
        help: "Configured value of one request in USD.",
        labels: {},
        value: this.options.valuePerRequest,
      });
    }
    if (this.options.revenuePerHour !== undefined) {
      samples.push({
        name: "rpc_xray_revenue_per_hour_usd",
        help: "Configured revenue per hour in USD.",
        labels: {},
        value: this.options.revenuePerHour,
      });
    }
    samples.push(
      {
        name: "rpc_xray_offers_enabled",
        help: "1 when offer messages are enabled.",
        labels: {},
        value: this.options.offers ? 1 : 0,
      },
      {
        name: "rpc_xray_info",
        help: "Package information.",
        labels: { version: VERSION, lang: "js" },
        value: 1,
      },
    );
    return samples;
  }
}
