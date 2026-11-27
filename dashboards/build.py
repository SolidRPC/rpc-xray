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


def stat(
    title,
    description,
    targets,
    unit,
    overrides=None,
    decimals=None,
    mappings=None,
    no_value=None,
    base_color=RED,
):
    defaults = {
        "unit": unit,
        "color": {"mode": "fixed", "fixedColor": base_color},
        "mappings": mappings or [],
    }
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
            "textMode": "value",
            "justifyMode": "auto",
            "orientation": "vertical",
            "text": {"valueSize": 36},
            "wideLayout": True,
        },
    }


def series(
    title,
    description,
    targets,
    unit,
    overrides=None,
    transformations=None,
    fill=10,
    stack=False,
    minimum=None,
    maximum=None,
):
    custom = {
        "drawStyle": "line",
        "lineWidth": 2,
        "fillOpacity": fill,
        "showPoints": "never",
        "spanNulls": False,
        "stacking": {"mode": "normal" if stack else "none"},
    }
    defaults = {
        "unit": unit,
        "custom": custom,
        "color": {"mode": "palette-classic-by-name"},
    }
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
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": True,
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def threshold_override(name, steps):
    return {
        "matcher": {"id": "byName", "options": name},
        "properties": [
            {"id": "color", "value": {"mode": "thresholds"}},
            {
                "id": "thresholds",
                "value": {
                    "mode": "absolute",
                    "steps": [{"color": c, "value": v} for c, v in steps],
                },
            },
        ],
    }


TARGET_LINE = [
    {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [8, 6]}},
    {"id": "custom.fillOpacity", "value": 0},
]


TRAFFIC = f"rpc_xray_requests_total{{{SEL}}}"
total = f"sum({TRAFFIC})"
failed = f"(sum(rpc_xray_requests_total{{{SEL}, {FAILED}}}) or (0 * {total}))"
caller = (
    f'(sum(rpc_xray_requests_total{{{SEL}, outcome="caller_error"}}) or (0 * {total}))'
)
success = f"({total} - {failed}) / {total}"
rate_window = "$__rate_interval"
latency = f"histogram_quantile(0.95, sum by (le) (rate(rpc_xray_request_duration_seconds_bucket{{{SEL}}}[{rate_window}])))"
latency_range = latency.replace("rate(", "increase(").replace(rate_window, "$__range")
live_alerts = 'ALERTS{alertstate="firing", rpc_xray="incident", ' + SEL + "}"
pending = live_alerts.replace('"firing"', '"pending"')
signal = f"count({TRAFFIC})"
monitored = f"({signal} and on () rpc_xray:incident_any)"
observed = "Current counters from the selected running exporters. Includes the first observation. Each exporter restarts its counters at zero. The time picker sets the evaluation endpoint."


def kpi(
    title,
    expr,
    unit,
    x,
    y,
    w,
    color,
    help_text,
    digits=None,
    empty="Unmeasured",
    mappings=None,
):
    add(
        stat(
            title,
            help_text,
            [target(expr, title, "A", True)],
            unit,
            decimals=digits,
            base_color=color,
            no_value=empty,
            mappings=mappings,
        ),
        x,
        y,
        w,
        4,
    )


def plot(title, expr, legend, unit, x, y, w, h, help_text, color=None):
    panel = series(
        title,
        help_text,
        [target(expr, legend, "A")],
        unit,
        [override(legend, color)] if color else [],
        minimum=0,
        fill=13,
    )
    add(panel, x, y, w, h)


add(
    {
        "type": "text",
        "title": "",
        "transparent": True,
        "options": {
            "mode": "markdown",
            "content": "## RPC X-Ray\nCounts cover active SDK processes. Charts follow the time range.",
        },
    },
    0,
    0,
    24,
    3,
)
kpi("Requests observed", total, "locale", 0, 3, 6, "#b9a4ff", observed, 0)
kpi(
    "Provider success",
    success,
    "percentunit",
    6,
    3,
    6,
    "#c4cbd6",
    observed
    + " Caller errors count as provider responses. Application success can be lower.",
    2,
)
kpi("Provider failures", failed, "locale", 12, 3, 6, "#e8b68e", observed, 0)
kpi("Caller errors", caller, "locale", 18, 3, 6, "#97b4cf", observed, 0)
panels[-2]["fieldConfig"]["overrides"] = [
    threshold_override("Provider failures", [(GREEN, None), (RED, 1)])
]

traffic_rate = f"sum by (method) (rate({TRAFFIC}[{rate_window}]))"
plot(
    "Traffic by method",
    traffic_rate,
    "{{method}}",
    "reqps",
    0,
    7,
    14,
    8,
    "RPC calls per second in the selected range. Batch members count separately.",
)
panels[-1]["fieldConfig"]["overrides"] = [
    override("eth_call", "#8eaff5"),
    override("eth_blockNumber", "#b9a4ff"),
    override("eth_getLogs", "#efb97c"),
]
add(
    series(
        "Response latency · p95",
        "Client-observed HTTP duration per provider. A batch has one duration. The dashed line is the configured target.",
        [
            target(
                f"histogram_quantile(0.95, sum by (le, provider) (rate(rpc_xray_request_duration_seconds_bucket{{{SEL}}}[{rate_window}])))",
                "{{provider}}",
                "A",
            ),
            target(f"vector($target_p95) and on () {signal}", "Target", "B"),
        ],
        "s",
        [override("Target", NEUTRAL, TARGET_LINE)],
        minimum=0,
        fill=9,
    ),
    14,
    7,
    10,
    8,
)

row("Incidents and chain head", 15)
kpi(
    "Firing incidents",
    f"count({live_alerts}) or (0 * {monitored})",
    "short",
    0,
    16,
    6,
    NEUTRAL,
    "Firing incident rules matching the selected chain and provider.",
    0,
)
panels[-1]["fieldConfig"]["overrides"] = [
    threshold_override("Firing incidents", [(GREEN, None), (RED, 1)])
]
kpi(
    "Pending incidents",
    f"count({pending}) or (0 * {monitored})",
    "short",
    6,
    16,
    6,
    "#c4cbd6",
    "Rules waiting for their configured hold time.",
    0,
)
kpi(
    "Time since head advanced",
    f"max(time() - rpc_xray_head_last_advance_timestamp_seconds{{{SEL}}})",
    "s",
    12,
    16,
    6,
    "#a9cddd",
    "Age of the oldest last advance among measured providers. Requires a recent head observation.",
    0,
    "Head unmeasured",
)
kpi(
    "Head lag · blocks",
    f"max(rpc_xray_head_lag_blocks{{{SEL}}} and on(job,instance,chain) (count by(job,instance,chain)(max by(job,instance,chain,provider)(rpc_xray_head_block)) > 1))",
    "short",
    18,
    16,
    6,
    "#a9cddd",
    "Blocks behind a peer on the same chain. Two measured providers on one chain in the same registry are required.",
    0,
    "Needs two heads",
)

timeline = f'max by (alertname, chain, provider) ({live_alerts}) or on () label_replace(0 * {monitored}, "alertname", "No firing incidents", "__name__", ".*")'
add(
    {
        "type": "state-timeline",
        "title": "Incident history",
        "description": "Each row is a firing incident for the selected providers. A blank period means telemetry was absent or the rule was inactive.",
        "targets": [target(timeline, "{{alertname}}  {{provider}}", "A")],
        "fieldConfig": {
            "defaults": {
                "custom": {"fillOpacity": 75, "lineWidth": 0, "spanNulls": False},
                "mappings": [
                    {
                        "type": "value",
                        "options": {
                            "0": {"text": "Quiet", "color": "#394652"},
                            "1": {"text": "Firing", "color": RED},
                        },
                    }
                ],
            },
            "overrides": [],
        },
        "options": {
            "showValue": "never",
            "rowHeight": 0.75,
            "mergeValues": True,
            "legend": {"showLegend": False},
            "tooltip": {"mode": "single"},
        },
    },
    0,
    20,
    14,
    7,
)
mix = f'sum by (outcome) (rpc_xray_requests_total{{{SEL},outcome!="ok"}}) or on () label_replace(0 * {total}, "outcome", "No errors", "__name__", ".*")'
add(
    {
        "type": "bargauge",
        "title": "Observed errors by outcome",
        "description": observed,
        "targets": [target(mix, "{{outcome}}", "A", True)],
        "fieldConfig": {
            "defaults": {
                "min": 0,
                "color": {"mode": "fixed", "fixedColor": "#b9a4ff"},
                "decimals": 0,
            },
            "overrides": [
                override("caller_error", "#97b4cf"),
                override("No errors", NEUTRAL),
            ],
        },
        "options": {
            "displayMode": "basic",
            "orientation": "horizontal",
            "showUnfilled": False,
            "reduceOptions": {"calcs": ["lastNotNull"], "values": False},
            "text": {"valueSize": 18, "titleSize": 13},
            "valueMode": "text",
        },
    },
    14,
    20,
    10,
    7,
)

row("Service targets and impact", 27)
window_total = f"sum(increase({TRAFFIC}[$__range]))"
add(
    series(
        "Provider success over time",
        "Rolling provider success. New low-volume series can be absent from rate estimates until two scrapes. The overview gives observed counters.",
        [
            target(f"rpc_xray:success_ratio:rate1h{{{SEL}}}", "{{provider}}", "A"),
            target(f"vector($target_success) and on () {signal}", "Target", "B"),
        ],
        "percentunit",
        [override("Target", NEUTRAL, TARGET_LINE)],
        fill=6,
        maximum=1,
    ),
    0,
    28,
    12,
    7,
)
plot(
    "Provider failures over time",
    f'sum by (outcome) (rate(rpc_xray_requests_total{{{SEL}, {FAILED}}}[{rate_window}])) or on () label_replace(0 * sum(rate({TRAFFIC}[{rate_window}])), "outcome", "No failures", "__name__", ".*")',
    "{{outcome}}",
    "reqps",
    12,
    28,
    12,
    7,
    "Estimated provider failures per second. Refer to observed errors for first events.",
)
kpi(
    "Excess wait · selected range",
    f"sum(increase(rpc_xray_slow_seconds_total{{{SEL}}}[$__range])) or (0 * {window_total})",
    "s",
    0,
    35,
    8,
    "#a9cddd",
    "Estimated cumulative time spent above one second per exchange.",
    1,
)
cost = f"sum((sum by(job,instance)(increase(rpc_xray_requests_total{{{SEL}, {FAILED}}}[$__range])) or (0 * sum by(job,instance)(increase({TRAFFIC}[$__range])))) * on(job,instance) rpc_xray_value_per_request_usd)"
kpi(
    "Estimated loss · selected range",
    cost,
    "currencyUSD",
    8,
    35,
    8,
    "#c4cbd6",
    "Estimated failures multiplied by each reporting instance’s configured value per request. Only configured instances contribute.",
    2,
    "Value unset",
)
kpi(
    "Response latency · range p95",
    latency_range,
    "s",
    16,
    35,
    8,
    "#b9a4ff",
    "95th percentile across selected HTTP exchanges during the dashboard time range.",
    2,
    "No timed traffic",
)

row("Fleet history and offer", 39)
weekly = "sum_over_time(rpc_xray:failed_requests:rate5m_total[7d]) * 300"
complete_week = f"({weekly}) and (count_over_time(rpc_xray:failed_requests:rate5m_total[7d]) >= 1996)"
add(
    stat(
        "Weekly failures · all providers",
        "Fleet estimates from five-minute samples. This week includes available samples. Showing the previous week or forecasting requires at least 99% of its five-minute samples.",
        [
            target(weekly, "This week", "A", True),
            target(
                complete_week.replace("[7d]", "[7d] offset 7d"),
                "Previous week",
                "B",
                True,
            ),
        ],
        "short",
        decimals=0,
        base_color="#c4cbd6",
        no_value="Collecting history",
    ),
    0,
    40,
    8,
    5,
)
panels[-1]["options"]["textMode"] = "value_and_name"
add(
    stat(
        "Projected failures · all providers",
        "Estimated failures in 30 and 90 days at the observed weekly rate.",
        [
            target(f"({complete_week}) * 30 / 7", "30 days", "A", True),
            target(f"({complete_week}) * 90 / 7", "90 days", "B", True),
        ],
        "short",
        decimals=0,
        base_color="#c4cbd6",
        no_value="Collecting history",
    ),
    8,
    40,
    8,
    5,
)
panels[-1]["options"]["textMode"] = "value_and_name"
kpi(
    "Incident minutes · fleet / 7d",
    "rpc_xray:incident_minutes:7d",
    "m",
    16,
    40,
    8,
    "#c4cbd6",
    "Fleet-wide time with a firing incident, in five-minute samples.",
    0,
    "Collecting history",
)
offer_panel = stat(
    "SolidRPC offer · fleet",
    "Eligible at 100M projected monthly requests with a recent incident and offers enabled. Select the active offer to email SolidRPC.",
    [
        target(
            'max(ALERTS{alertname="RpcXrayTrialOffer",alertstate="firing"}) and on () (max(rpc_xray_offers_enabled) == 1)',
            "Offer",
            "A",
            True,
        )
    ],
    "none",
    base_color="#b9a4ff",
    no_value="No active offer",
    mappings=[
        {
            "type": "value",
            "options": {
                "1": {
                    "text": "14-day trial or $100 credit  ·  hello@solidrpc.io",
                    "color": "#b9a4ff",
                }
            },
        }
    ],
)
offer_panel["fieldConfig"]["defaults"]["links"] = [
    {
        "title": "Request the RPC X-Ray offer",
        "url": "mailto:hello@solidrpc.io?subject=RPC%20X-Ray%20offer",
    }
]
offer_panel["options"]["text"] = {"valueSize": 22}
add(offer_panel, 0, 45, 24, 3)


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
    return {
        "type": "textbox",
        "name": name,
        "label": label,
        "query": value,
        "current": {"text": value, "value": value},
        "options": [{"selected": True, "text": value, "value": value}],
    }


dashboard = {
    "uid": "rpc-xray",
    "title": "RPC X-Ray · Request health",
    "description": "See every request your RPC provider failed.",
    "tags": ["rpc-xray"],
    "schemaVersion": 39,
    "version": 2,
    "editable": True,
    "graphTooltip": 1,
    "time": {"from": "now-30m", "to": "now"},
    "refresh": "30s",
    "timezone": "browser",
    "templating": {
        "list": [
            {
                "type": "datasource",
                "name": "datasource",
                "hide": 0,
                "label": "Source",
                "query": "prometheus",
                "current": {},
            },
            query_variable(
                "chain", "Chain", "label_values(rpc_xray_requests_total, chain)"
            ),
            query_variable(
                "provider",
                "Provider",
                'label_values(rpc_xray_requests_total{chain=~"$chain"}, provider)',
            ),
            text_variable("target_success", "Success target", "0.9999"),
            text_variable("target_p95", "p95 target (s)", "0.3"),
        ]
    },
    "panels": panels,
}

path = pathlib.Path(__file__).with_name("rpc-xray.json")
path.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"wrote {path.name}: {len(panels)} panels")
