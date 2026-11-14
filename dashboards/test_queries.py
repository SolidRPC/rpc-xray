import json
from pathlib import Path

dashboard = json.loads(Path(__file__).with_name("rpc-xray.json").read_text())
panels = {panel["title"]: panel for panel in dashboard["panels"]}


def query(title, index=0):
    expression = panels[title]["targets"][index]["expr"]
    for variable, value in [("$__range", "5m"), ("$__interval", "1m"), ("$chain", ".*"), ("$provider", ".*")]:
        expression = expression.replace(variable, value)
    return expression


healthy = {"series": 'rpc_xray_requests_total{chain="ethereum", provider="healthy.example", method="eth_call", outcome="ok"}', "values": "0+30x20"}
failure = {"series": 'rpc_xray_requests_total{chain="ethereum", provider="healthy.example", method="eth_call", outcome="http_error"}', "values": "0+30x20"}


def check(title, samples, index=0):
    return {"expr": query(title, index), "eval_time": "10m", "exp_samples": samples}


def sample(value, labels="{}"):
    return {"labels": labels, "value": value}


tests = [
    {
        "name": "healthy traffic without failure series and disabled probes",
        "interval": "30s",
        "input_series": [healthy],
        "promql_expr_test": [
            check("Success rate", [sample(1)]),
            check("Failed user requests", [sample(0)]),
            check("Time users waited beyond 1 s", [sample(0)]),
            check("Failed user requests, adding up", [sample(0)]),
            check("Failed requests per second, by kind", [sample(0, '{outcome="No failures"}')]),
            check("Worst head lag right now", []),
            check("Seconds since the last new block", [], 1),
            check("Open incidents", [sample(0, '{alertname="No open incidents"}')]),
        ],
    },
    {
        "name": "failures are preserved rather than replaced with zero",
        "interval": "30s",
        "input_series": [healthy, failure],
        "promql_expr_test": [
            check("Success rate", [sample(0.5)]),
            check("Failed user requests", [sample(300)]),
            check("Failed requests per second, by kind", [sample(1, '{outcome="http_error"}')]),
        ],
    },
    {
        "name": "missing telemetry is not reported as healthy",
        "promql_expr_test": [check(title, []) for title in ["Success rate", "Failed user requests", "Time users waited beyond 1 s", "Failed user requests, adding up", "Failed requests per second, by kind", "Worst head lag right now"]],
    },
    {
        "name": "active incidents keep their labels and suppress the empty row",
        "interval": "30s",
        "input_series": [{"series": 'ALERTS{alertstate="firing", rpc_xray="incident", alertname="RpcXrayHttpErrors", chain="ethereum", provider="failing.example"}', "values": "1x20"}],
        "promql_expr_test": [check("Open incidents", [sample(1, '{alertname="RpcXrayHttpErrors", chain="ethereum", provider="failing.example"}')])],
    },
]

print(json.dumps({"tests": tests}, indent=2))
