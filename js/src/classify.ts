import { CALLER_CODES, RATE_PATTERNS, CALLER_PATTERNS } from "./contract.js";
import { methodLabel } from "./methods.js";

export type Outcome =
  "ok" | "http_error" | "rate_limited" | "rpc_error" | "caller_error" | "timeout" | "network";

export interface Entry {
  method: string;
  outcome: Outcome;
}

export interface ParsedRequest {
  batch: boolean;
  calls: { id: unknown; method: string }[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseRequest(body: string): ParsedRequest | undefined {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return undefined;
  }
  if (Array.isArray(parsed)) {
    const calls: ParsedRequest["calls"] = [];
    for (const item of parsed) {
      if (!isRecord(item) || typeof item.method !== "string") return undefined;
      calls.push({ id: item.id, method: methodLabel(item.method) });
    }
    return calls.length > 0 ? { batch: true, calls } : undefined;
  }
  if (!isRecord(parsed) || typeof parsed.method !== "string") return undefined;
  return { batch: false, calls: [{ id: parsed.id, method: methodLabel(parsed.method) }] };
}

export function classifyRpcError(error: unknown): Outcome {
  const code = isRecord(error) && typeof error.code === "number" ? error.code : undefined;
  const message =
    isRecord(error) && typeof error.message === "string" ? error.message.toLowerCase() : "";
  if (code === 429 || RATE_PATTERNS.some((pattern) => message.includes(pattern))) {
    return "rate_limited";
  }
  if (
    (code !== undefined && CALLER_CODES.has(code)) ||
    CALLER_PATTERNS.some((pattern) => message.includes(pattern))
  ) {
    return "caller_error";
  }
  return "rpc_error";
}

function outcomeOfResponseObject(value: unknown): Outcome {
  if (!isRecord(value)) return "rpc_error";
  if (value.error !== undefined && value.error !== null) return classifyRpcError(value.error);
  return "result" in value ? "ok" : "rpc_error";
}

function all(request: ParsedRequest, outcome: Outcome): Entry[] {
  return request.calls.map((call) => ({ method: call.method, outcome }));
}

export function classifyFailure(request: ParsedRequest, kind: "timeout" | "network"): Entry[] {
  return all(request, kind);
}

export function classifyResponse(
  request: ParsedRequest,
  status: number,
  body: string | undefined,
): Entry[] {
  if (status === 429) return all(request, "rate_limited");
  if (status >= 400) return all(request, "http_error");
  if (body === undefined) return all(request, "ok");
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return all(request, "rpc_error");
  }
  if (!Array.isArray(parsed)) return all(request, outcomeOfResponseObject(parsed));
  const byId = new Map<string, Outcome>();
  for (const item of parsed) {
    if (isRecord(item)) byId.set(JSON.stringify(item.id ?? null), outcomeOfResponseObject(item));
  }
  return request.calls.map((call) => ({
    method: call.method,
    outcome: byId.get(JSON.stringify(call.id ?? null)) ?? "rpc_error",
  }));
}
