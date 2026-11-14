#!/usr/bin/env python3
import json
import pathlib

DS = {"type": "prometheus", "uid": "${datasource}"}
FAILED = 'outcome=~"http_error|rate_limited|rpc_error|timeout|network"'
SEL = 'chain=~"$chain", provider=~"$provider"'
RED = "#e5484d"
GREEN = "#30a46c"
AMBER = "#f5a524"
VIOLET = "#7c3aed"
NEUTRAL = "#747c89"

panels = []
next_id = [1]


def target(expr, legend, ref, instant=False):
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "instant": instant,
        "range": not instant,
    }


def add(panel, x, y, w, h):
    panel["id"] = next_id[0]
    next_id[0] += 1
    panel["gridPos"] = {"x": x, "y": y, "w": w, "h": h}
    panel.setdefault("datasource", DS)
    panels.append(panel)


def row(title, y):
    add({"type": "row", "title": title, "collapsed": False, "panels": []}, 0, y, 24, 1)


def override(name, color, extra=None):
    properties = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
    properties.extend(extra or [])
    return {"matcher": {"id": "byName", "options": name}, "properties": properties}


def stat(title, description, targets, unit, overrides=None, decimals=None, mappings=None, no_value=None, base_color=RED):
    defaults = {"unit": unit, "color": {"mode": "fixed", "fixedColor": base_color}, "mappings": mappings or []}
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value is not None:
        defaults["noValue"] = no_value
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value_and_name",
            "justifyMode": "center",
            "orientation": "vertical",
            "text": {"valueSize": 26, "titleSize": 12},
        },
    }


def series(title, description, targets, unit, overrides=None, transformations=None, fill=10, stack=False, minimum=None, maximum=None):
    custom = {
        "drawStyle": "line",
        "lineWidth": 2,
        "fillOpacity": fill,
        "showPoints": "never",
        "spanNulls": True,
        "stacking": {"mode": "normal" if stack else "none"},
    }
    defaults = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "targets": targets,
        "transformations": transformations or [],
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def cumulative(names):
    steps = [{"id": "joinByField", "options": {"byField": "Time", "mode": "outer"}}]
    for source, alias in names.items():
        steps.append(
            {
                "id": "calculateField",
                "options": {
                    "mode": "cumulativeFunctions",
                    "cumulative": {"field": source, "reducer": "sum"},
                    "alias": alias,
                    "replaceFields": False,
                },
            }
        )
    steps.append({"id": "organize", "options": {"excludeByName": {source: True for source in names}}})
    return steps


def threshold_override(name, steps):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [
            {"id": "color", "value": {"mode": "thresholds"}},
            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in steps]}},
        ],
    }


TARGET_LINE = [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}}, {"id": "custom.fillOpacity", "value": 0}]

total_range = f"sum(increase(rpc_xray_requests_total{{{SEL}}}[$__range]))"
failed_range = f"(sum(increase(rpc_xray_requests_total{{{SEL}, {FAILED}}}[$__range])) or (0 * {total_range}))"
you_success = f"1 - {failed_range} / {total_range}"
you_p95 = f"histogram_quantile(0.95, sum by (le) (increase(rpc_xray_request_duration_seconds_bucket{{{SEL}}}[$__range])))"
unknown = [{"type": "special", "options": {"match": "null", "result": {"text": "No data", "color": NEUTRAL}}}, {"type": "special", "options": {"match": "nan", "result": {"text": "No traffic", "color": NEUTRAL}}}]

row("Measured traffic and configured targets", 0)
add(
    stat(
        "Success rate",
        "Share of your real RPC requests that got a usable answer in the selected time range. Reverts and invalid requests are your own results and are not counted as failures.",
        [target(you_success, "You", "A", True), target("vector($target_success)", "Configured target", "B", True)],
        "percentunit",
        [override("Configured target", VIOLET), threshold_override("You", [(RED, None), (GREEN, 0.9999)])],
        decimals=2,
        mappings=unknown,
        no_value="No data",
    ),
    0, 1, 6, 6,
)
add(
    stat(
        "p95 latency",
        "One request in twenty was slower than this in the selected time range.",
        [target(you_p95, "You", "A", True), target("vector($target_p95)", "Configured target", "B", True)],
        "s",
        [override("Configured target", VIOLET), threshold_override("You", [(GREEN, None), (RED, 0.3)])],
        decimals=2,
        mappings=unknown,
        no_value="No data",
    ),
    6, 1, 6, 6,
)
add(
    stat(
        "Minutes with an open incident",
        "Minutes in the selected time range when at least one RPC incident was open: a stale head, a lagging head, failing requests or slow answers.",
        [
            target("sum_over_time(rpc_xray:incident_any:max5m[$__range]) * 5 or vector(0)", "You", "A", True),
            target("vector(0)", "Configured target", "B", True),
        ],
        "m",
        [override("Configured target", VIOLET), threshold_override("You", [(GREEN, None), (RED, 1)])],
        decimals=0,
    ),
    12, 1, 6, 6,
)
add(
    stat(
        "Worst head lag right now",
        "How far the slowest endpoint is behind the best source of the same chain. Not measured when probes are disabled or have not established a moving head. A zero from one endpoint alone cannot establish freshness.",
        [
            target(f"max(rpc_xray:head_lag_seconds{{{SEL}}})", "You", "A", True),
        ],
        "s",
        [threshold_override("You", [(GREEN, None), (RED, 1)])],
        decimals=0,
        no_value="Not measured",
        base_color=NEUTRAL,
        mappings=[{"type": "special", "options": {"match": "null", "result": {"text": "Not measured", "color": NEUTRAL}}}],
    ),
    18, 1, 6, 6,
)

row("What waiting has cost so far", 7)
add(
    series(
        "Failed user requests, adding up",
        "Every failed request since the start of the selected time range. The red line only climbs. The violet line is how many failures the same traffic would have had at the configured target success rate.",
        [
            target(f"sum(increase(rpc_xray_requests_total{{{SEL}, {FAILED}}}[$__interval])) or (0 * sum(increase(rpc_xray_requests_total{{{SEL}}}[$__interval])))", "failed", "A"),
            target(f"sum(increase(rpc_xray_requests_total{{{SEL}}}[$__interval])) * (1 - $target_success)", "target", "B"),
        ],
        "short",
        [override("Failed user requests", RED), override("Configured target", VIOLET, TARGET_LINE)],
        transformations=cumulative({"failed": "Failed user requests", "target": "Configured target"}),
        fill=18,
        minimum=0,
    )
    | {"interval": "1m"},
    0, 8, 14, 9,
)
add(
    stat(
        "Failed user requests",
        "Requests in the selected time range that returned an HTTP error, were rate limited, timed out, never connected, or came back with a provider error at HTTP 200.",
        [target(failed_range, "Failed", "A", True)],
        "short",
        [threshold_override("Failed", [(GREEN, None), (RED, 0.000001)])],
        decimals=0,
        no_value="No data",
    ),
    14, 8, 5, 3,
)
add(
    stat(
        "Time users waited beyond 1 s",
        "Total waiting time above one second per request in the selected time range.",
        [target(f"sum(increase(rpc_xray_slow_seconds_total{{{SEL}}}[$__range])) or (0 * {total_range})", "Waited", "A", True)],
        "s",
        [threshold_override("Waited", [(GREEN, None), (RED, 0.000001)])],
        decimals=0,
        no_value="No data",
    ),
    19, 8, 5, 3,
)
add(
    stat(
        "Estimated loss",
        "Failed requests multiplied by the value of one request. Shown only when valuePerRequest is set in rpc-xray.",
        [target(f"({failed_range}) * on () max(rpc_xray_value_per_request_usd)", "Lost", "A", True)],
        "currencyUSD",
        decimals=0,
        no_value="Not configured",
        base_color=NEUTRAL,
    ),
    14, 11, 5, 3,
)
add(
    stat(
        "Failures this week against last week",
        "Failed requests in the last 7 days and in the 7 days before that.",
        [
            target("sum_over_time(rpc_xray:failed_requests:rate5m_total[7d]) * 300", "This week", "A", True),
            target("sum_over_time(rpc_xray:failed_requests:rate5m_total[7d] offset 7d) * 300", "Last week", "B", True),
        ],
        "short",
        [override("Last week", AMBER)],
        decimals=0,
        no_value="Collecting history",
        base_color=NEUTRAL,
    ),
    19, 11, 5, 3,
)
add(
    stat(
        "If nothing changes: next 30 and 90 days",
        "A straight-line projection from the last 7 days. This is how many more user requests fail if the RPC setup stays as it is.",
        [
            target("sum_over_time(rpc_xray:failed_requests:rate5m_total[7d]) * 300 * 30 / 7", "Next 30 days", "A", True),
            target("sum_over_time(rpc_xray:failed_requests:rate5m_total[7d]) * 300 * 90 / 7", "Next 90 days", "B", True),
        ],
        "short",
        decimals=0,
        no_value="Collecting history",
        base_color=NEUTRAL,
    ),
    14, 14, 10, 3,
)

row("Incidents", 17)
add(
    {
        "type": "state-timeline",
        "title": "Open incidents",
        "description": "Firing alert rules over time. No open incidents is an alert state, not proof that traffic or head measurements are available.",
        "targets": [target('max by (alertname, chain, provider) (ALERTS{alertstate="firing", rpc_xray="incident", ' + SEL + '}) or on () label_replace(vector(0), "alertname", "No open incidents", "__name__", ".*")', "{{alertname}} {{chain}} {{provider}}", "A")],
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "fixed", "fixedColor": RED},
                "custom": {"fillOpacity": 85, "lineWidth": 0, "spanNulls": False},
                "mappings": [{"type": "value", "options": {"0": {"text": "No open incidents", "color": NEUTRAL}, "1": {"text": "Open", "color": RED}}}],
            },
            "overrides": [],
        },
        "options": {"showValue": "never", "mergeValues": True, "rowHeight": 0.8, "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
    },
    0, 18, 16, 7,
)
add(
    stat(
        "SolidRPC offer",
        "Shown when you send about 100M requests a month and had at least one incident in the last 7 days. Set offers: false in rpc-xray to turn it off.",
        [target('max(ALERTS{alertname="RpcXrayTrialOffer", alertstate="firing"})', "Offer", "A", True)],
        "none",
        [override("Offer", VIOLET)],
        mappings=[{"type": "value", "options": {"1": {"text": "14-day free trial or $100 credit. Email hello@solidrpc.io", "color": VIOLET}}}],
        no_value="No active offer",
        base_color="#747c89",
    ),
    16, 18, 8, 7,
)

row("By provider", 25)
add(
    series(
        "Success rate by provider",
        "Hourly success rate per endpoint, against the target.",
        [
            target(f"rpc_xray:success_ratio:rate1h{{{SEL}}}", "{{chain}} {{provider}}", "A"),
            target("vector($target_success)", "Configured target", "B"),
        ],
        "percentunit",
        [override("Configured target", VIOLET, TARGET_LINE)],
        fill=0,
        maximum=1,
    ),
    0, 26, 12, 8,
)
add(
    series(
        "p95 latency by provider",
        "The dashed red line is the 1 second incident threshold. The violet line is the configured target.",
        [
            target(f"rpc_xray:latency_seconds:p95_1m{{{SEL}}}", "{{chain}} {{provider}}", "A"),
            target("vector(1)", "Incident threshold", "B"),
            target("vector($target_p95)", "Configured target", "C"),
        ],
        "s",
        [override("Incident threshold", RED, TARGET_LINE), override("Configured target", VIOLET, TARGET_LINE)],
        fill=0,
        minimum=0,
    ),
    12, 26, 12, 8,
)
add(
    series(
        "Failed requests per second, by kind",
        "What kind of failure your users hit.",
        [target(f'sum by (outcome) (rate(rpc_xray_requests_total{{{SEL}, {FAILED}}}[5m])) or on () label_replace(0 * sum(rate(rpc_xray_requests_total{{{SEL}}}[5m])), "outcome", "No failures", "__name__", ".*")', "{{outcome}}", "A")],
        "reqps",
        fill=30,
        stack=True,
        minimum=0,
    ),
    0, 34, 12, 8,
)
add(
    series(
        "Seconds since the last new block",
        "A rising line means no new block has been observed. Requires head probes. No data means unmeasured, including when probeIntervalSeconds is 0. The dashed line is the 3 minute incident threshold.",
        [
            target(f"rpc_xray:head_stale_seconds{{{SEL}}}", "{{chain}} {{provider}}", "A"),
            target(f"vector(180) and on () rpc_xray:head_stale_seconds{{{SEL}}}", "Incident threshold", "B"),
        ],
        "s",
        [override("Incident threshold", RED, TARGET_LINE)],
        fill=0,
        minimum=0,
    ),
    12, 34, 12, 8,
)
add(
    series(
        "Requests per second by method",
        "Your real traffic, by JSON-RPC method.",
        [target(f"sum by (method) (rate(rpc_xray_requests_total{{{SEL}}}[5m]))", "{{method}}", "A")],
        "reqps",
        fill=20,
        stack=True,
        minimum=0,
    ),
    0, 42, 24, 8,
)


def query_variable(name, label, query):
    return {
        "type": "query",
        "name": name,
        "label": label,
        "datasource": DS,
        "query": {"query": query, "refId": "A"},
        "definition": query,
        "includeAll": True,
        "multi": True,
        "allValue": ".*",
        "current": {"text": "All", "value": "$__all"},
        "refresh": 2,
        "sort": 1,
    }


def text_variable(name, label, value):
    return {"type": "textbox", "name": name, "label": label, "query": value, "current": {"text": value, "value": value}, "options": [{"selected": True, "text": value, "value": value}]}


dashboard = {
    "uid": "rpc-xray",
    "title": "rpc-xray",
    "description": "See every request your RPC provider failed.",
    "tags": ["rpc-xray"],
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "graphTooltip": 1,
    "time": {"from": "now-24h", "to": "now"},
    "refresh": "30s",
    "timezone": "browser",
    "templating": {
        "list": [
            {"type": "datasource", "name": "datasource", "label": "Data source", "query": "prometheus", "current": {}},
            query_variable("chain", "Chain", "label_values(rpc_xray_requests_total, chain)"),
            query_variable("provider", "Provider", 'label_values(rpc_xray_requests_total{chain=~"$chain"}, provider)'),
            text_variable("target_success", "Target success rate", "0.9999"),
            text_variable("target_p95", "Target p95 (s)", "0.3"),
        ]
    },
    "panels": panels,
}

path = pathlib.Path(__file__).with_name("rpc-xray.json")
path.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {path.name}: {len(panels)} panels")
