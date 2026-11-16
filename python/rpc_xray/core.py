import json
import math
import threading
import time
from urllib.parse import urlsplit

from ._contract import (
    BUCKETS,
    CALLER_CODES,
    CALLER_PATTERNS,
    CHAINS,
    METHODS,
    RATE_PATTERNS,
)

VERSION = "0.1.0"
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def chain_label(chain_id):
    return CHAINS.get(str(chain_id), f"eip155:{chain_id}")


def _invalid_constant(_):
    raise ValueError("Invalid JSON constant")


def _id_key(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_request(body):
    try:
        payload = json.loads(body, parse_constant=_invalid_constant)
        calls = payload if isinstance(payload, list) else [payload]
        if not calls or any(
            not isinstance(c, dict) or not isinstance(c.get("method"), str)
            for c in calls
        ):
            return None
        return isinstance(payload, list), [
            (
                _id_key(c.get("id")),
                c["method"] if c["method"] in METHODS else "other",
            )
            for c in calls
        ]
    except (ValueError, TypeError, RecursionError):
        return None


def rpc_error(error):
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    message = error.get("message", "")
    message = message.lower() if isinstance(message, str) else ""
    if code == 429 or any(p in message for p in RATE_PATTERNS):
        return "rate_limited"
    if (isinstance(code, (int, float)) and code in CALLER_CODES) or any(
        p in message for p in CALLER_PATTERNS
    ):
        return "caller_error"
    return "rpc_error"


def classify(request, status=200, body=None, failure=None):
    if request is None:
        return []

    def all_calls(outcome):
        return [{"method": method, "outcome": outcome} for _, method in request[1]]

    if failure:
        return all_calls(failure)
    if status == 429:
        return all_calls("rate_limited")
    if status >= 400:
        return all_calls("http_error")
    if body is None:
        return all_calls("ok")
    try:
        payload = json.loads(body, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError):
        return all_calls("rpc_error")

    def outcome(item):
        if not isinstance(item, dict):
            return "rpc_error"
        if item.get("error") is not None:
            return rpc_error(item["error"])
        return "ok" if "result" in item else "rpc_error"

    if not isinstance(payload, list):
        return all_calls(outcome(payload))
    by_id = {
        _id_key(item.get("id")): outcome(item)
        for item in payload
        if isinstance(item, dict)
    }
    return [
        {"method": method, "outcome": by_id.get(key, "rpc_error")}
        for key, method in request[1]
    ]


def _labels(labels):
    def escape(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return (
        "{" + ",".join(f'{key}="{escape(value)}"' for key, value in labels) + "}"
        if labels
        else ""
    )


class Observation:
    def __init__(self, xray, base, request):
        self.xray, self.base, self.request = xray, base, request
        self.started = time.monotonic()
        self.done = False

    def finish(self, status=200, body=None, failure=None):
        try:
            with self.xray._lock:
                if self.done:
                    return
                self.done = True
                if body is not None and len(body) > self.xray.max_body_bytes:
                    body = None
                entries = classify(self.request, status, body, failure)
                seconds = max(0, time.monotonic() - self.started)
                for entry in entries:
                    self.xray._inc(
                        "rpc_xray_requests_total",
                        self.base
                        + (("method", entry["method"]), ("outcome", entry["outcome"])),
                    )
                if not entries:
                    return
                labels = self.base + (
                    ("method", "batch" if self.request[0] else entries[0]["method"]),
                )
                key = ("rpc_xray_request_duration_seconds", labels)
                if key in self.xray._histograms or self.xray._size() < 5000:
                    h = self.xray._histograms.setdefault(
                        key, [0, 0.0, [0] * len(BUCKETS)]
                    )
                    h[0] += 1
                    h[1] += seconds
                    for i, bucket in enumerate(BUCKETS):
                        h[2][i] += seconds <= bucket
                if seconds > 1:
                    self.xray._inc(
                        "rpc_xray_slow_seconds_total", self.base, seconds - 1
                    )
        except Exception:
            pass


class Xray:
    def __init__(
        self,
        *,
        max_body_bytes=1024 * 1024,
        value_per_request=None,
        revenue_per_hour=None,
    ):
        if not isinstance(max_body_bytes, int) or max_body_bytes < 1:
            raise ValueError("max_body_bytes must be a positive integer")
        for value in (value_per_request, revenue_per_hour):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("monetary values must be finite and nonnegative")
        self.max_body_bytes = max_body_bytes
        self.value_per_request, self.revenue_per_hour = (
            value_per_request,
            revenue_per_hour,
        )
        self._lock = threading.RLock()
        self._endpoints, self._counters, self._histograms, self._heads = {}, {}, {}, {}

    def _size(self):
        return len(self._counters) + len(self._histograms)

    def _inc(self, name, labels, value=1):
        key = (name, labels)
        if key in self._counters or self._size() < 5000:
            self._counters[key] = self._counters.get(key, 0) + value

    def _endpoint(self, url, chain):
        key = (url, chain)
        if key not in self._endpoints:
            if len(self._endpoints) >= 32:
                return (("chain", "unknown"), ("provider", "other"))
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("HTTP URL required")
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            if parsed.port and parsed.port != (443 if parsed.scheme == "https" else 80):
                host += f":{parsed.port}"
            self._endpoints[key] = (("chain", chain or "unknown"), ("provider", host))
        return self._endpoints[key]

    def start(self, url, chain, body):
        try:
            if len(body) > self.max_body_bytes:
                return None
            request = parse_request(body)
            if request is None:
                return None
            with self._lock:
                return Observation(self, self._endpoint(str(url), chain), request)
        except Exception:
            return None

    def record_probe(self, url, chain, head=None):
        try:
            with self._lock:
                base = self._endpoint(str(url), chain)
                valid = (
                    isinstance(head, int) and not isinstance(head, bool) and head >= 0
                )
                self._inc(
                    "rpc_xray_probe_requests_total",
                    base + (("result", "ok" if valid else "error"),),
                )
                if valid and dict(base)["provider"] != "other":
                    previous = self._heads.get(base)
                    if previous is None or head > previous[0]:
                        self._heads[base] = (head, time.time())
        except Exception:
            pass

    def metrics(self):
        with self._lock:
            lines, seen = [], set()

            def sample(name, kind, labels, value):
                if name not in seen:
                    seen.add(name)
                    lines.extend(
                        [
                            f"# HELP {name} RPC X-Ray {name.removeprefix('rpc_xray_')}.",
                            f"# TYPE {name} {kind}",
                        ]
                    )
                lines.append(f"{name}{_labels(labels)} {value}")

            for (name, labels), value in sorted(self._counters.items()):
                sample(name, "counter", labels, value)
            for (name, labels), (count, total, buckets) in sorted(
                self._histograms.items()
            ):
                if name not in seen:
                    seen.add(name)
                    lines.extend(
                        [
                            f"# HELP {name} Duration of JSON-RPC HTTP requests.",
                            f"# TYPE {name} histogram",
                        ]
                    )
                for bound, count_at_bound in zip(BUCKETS + ["+Inf"], buckets + [count]):
                    lines.append(
                        f"{name}_bucket{_labels(labels + (('le', str(bound)),))} {count_at_bound}"
                    )
                lines.extend(
                    [
                        f"{name}_sum{_labels(labels)} {total}",
                        f"{name}_count{_labels(labels)} {count}",
                    ]
                )
            best = {}
            for base, (head, _) in self._heads.items():
                chain = dict(base)["chain"]
                best[chain] = max(best.get(chain, 0), head)
            for base, (head, advanced) in sorted(self._heads.items()):
                for name, value in (
                    ("head_block", head),
                    ("head_last_advance_timestamp_seconds", advanced),
                    ("head_age_seconds", max(0, time.time() - advanced)),
                    ("head_lag_blocks", best[dict(base)["chain"]] - head),
                ):
                    sample("rpc_xray_" + name, "gauge", base, value)
            for name, value in (
                ("value_per_request_usd", self.value_per_request),
                ("revenue_per_hour_usd", self.revenue_per_hour),
            ):
                if value is not None:
                    sample("rpc_xray_" + name, "gauge", (), value)
            sample("rpc_xray_offers_enabled", "gauge", (), 0)
            sample(
                "rpc_xray_info", "gauge", (("version", VERSION), ("lang", "python")), 1
            )
            return "\n".join(lines) + "\n"
