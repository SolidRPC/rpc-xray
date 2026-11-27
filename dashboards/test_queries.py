import json
from pathlib import Path

dashboard = json.loads(Path(__file__).with_name("rpc-xray.json").read_text())
panels = {panel["title"]: panel for panel in dashboard["panels"]}


def check(title, values, *, ref=0, chain=".*", provider=".*"):
    expr = panels[title]["targets"][ref]["expr"]
    for name, value in {
        "$__range": "5m",
        "$__rate_interval": "1m",
        "$chain": chain,
        "$provider": provider,
        "$target_success": "0.9999",
        "$target_p95": "0.3",
    }.items():
        expr = expr.replace(name, value)
    return {
        "expr": expr,
        "eval_time": "10m",
        "exp_samples": [{"labels": labels, "value": value} for labels, value in values],
    }


def count(outcome, values, provider="west.test"):
    labels = (
        f'chain="ethereum",provider="{provider}",method="eth_call",outcome="{outcome}"'
    )
    return {"series": f"rpc_xray_requests_total{{{labels}}}", "values": values}


def scalar(value):
    return [("{}", value)]


def test(name, inputs, checks):
    return {
        "name": name,
        "interval": "30s",
        "input_series": inputs,
        "promql_expr_test": checks,
    }


ok = count("ok", "12x20")
first = "_x18 1 1"
outcomes = ["http_error", "timeout", "rpc_error"]
tests = [
    test(
        "first failures count even if each counter was first scraped at one",
        [ok]
        + [count(outcome, first) for outcome in outcomes]
        + [count("ok", "100x20", "north.test")],
        [
            check("Requests observed", scalar(15), provider="west.test"),
            check("Provider failures", scalar(3), provider="west.test"),
            check("Provider success", scalar(0.8), provider="west.test"),
            check(
                "Observed errors by outcome",
                [(f'{{outcome="{s}"}}', 1) for s in outcomes],
            ),
            check("Provider failures", scalar(0), provider="north.test"),
        ],
    ),
    test(
        "caller rejections are visible separately from provider faults",
        [count("caller_error", "2x20")],
        [check("Provider success", scalar(1)), check("Caller errors", scalar(2))],
    ),
    test(
        "a healthy observed provider has zero failures and no measured peer lag",
        [ok, {"series": "rpc_xray:incident_any", "values": "0x20"}],
        [
            check("Provider failures", scalar(0)),
            check("Firing incidents", scalar(0)),
            check("Head lag · blocks", []),
            check("Time since head advanced", []),
            check("Estimated loss · selected range", []),
            check("Incident history", [('{alertname="No firing incidents"}', 0)]),
        ],
    ),
    test(
        "data without the incident rule group cannot establish alert health",
        [ok],
        [
            check("Firing incidents", []),
            check("Pending incidents", []),
            check("Incident history", []),
        ],
    ),
    test(
        "an empty selection stays unmeasured",
        [],
        [
            check(t, [])
            for t in [
                "Requests observed",
                "Provider success",
                "Provider failures",
                "Firing incidents",
                "Incident history",
                "Time since head advanced",
                "Projected failures · all providers",
                "SolidRPC offer · fleet",
            ]
        ],
    ),
    test(
        "restart reflects active process totals",
        [count("ok", "100x18 0 2"), count("http_error", "10x18 0 1")],
        [
            check("Requests observed", scalar(3)),
            check("Provider failures", scalar(1)),
            check("Provider success", scalar(2 / 3)),
        ],
    ),
    test(
        "visible alert labels and opt out",
        [
            ok,
            {
                "series": 'ALERTS{alertname="RpcXraySlow",alertstate="firing",rpc_xray="incident",chain="ethereum",provider="west.test"}',
                "values": "1x20",
            },
            {
                "series": 'ALERTS{alertname="RpcXrayTrialOffer",alertstate="firing"}',
                "values": "1x20",
            },
            {"series": "rpc_xray_offers_enabled", "values": "0x20"},
        ],
        [
            check("Firing incidents", scalar(1)),
            check(
                "Incident history",
                [
                    (
                        '{alertname="RpcXraySlow",chain="ethereum",provider="west.test"}',
                        1,
                    )
                ],
            ),
            check("Firing incidents", [], provider="north.test"),
            check("SolidRPC offer · fleet", []),
        ],
    ),
]


def head(provider, instance, value):
    return {
        "series": f'rpc_xray_head_block{{chain="ethereum",provider="{provider}",instance="{instance}"}}',
        "values": f"{value}x20",
    }


lag = {
    "series": 'rpc_xray_head_lag_blocks{chain="ethereum",provider="west.test",instance="one"}',
    "values": "3x20",
}
tests.extend(
    [
        test(
            "two heads in one exporter permit a peer comparison",
            [head("west.test", "one", 10), head("north.test", "one", 13), lag],
            [check("Head lag · blocks", scalar(3), provider="west.test")],
        ),
        test(
            "heads in independent registries do not imply a measured peer lag",
            [head("west.test", "one", 10), head("north.test", "two", 13), lag],
            [check("Head lag · blocks", [])],
        ),
        test(
            "time series distinguish measured zero from missing data",
            [count("ok", "0+30x20")],
            [
                check("Provider failures over time", [('{outcome="No failures"}', 0)]),
                check("Excess wait · selected range", [("{}", 0)]),
            ],
        ),
        test(
            "failures stay visible in the time series",
            [count("ok", "0+30x20"), count("http_error", "0+30x20")],
            [check("Provider failures over time", [('{outcome="http_error"}', 1)])],
        ),
    ]
)
tests.append(
    test(
        "all panel expressions accept an empty datasource",
        [],
        [
            check(title, [], ref=i)
            for title, panel in panels.items()
            for i, _ in enumerate(panel.get("targets", []))
        ],
    )
)
print(json.dumps({"tests": tests}))
