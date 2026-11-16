import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]
schema = json.loads((root / "spec/metrics.json").read_text())
contract = json.loads((root / "spec/contract.json").read_text())
text = Path(sys.argv[1]).read_text()
types = dict(re.findall(r"^# TYPE (\w+) (\w+)$", text, re.M))
assert types == {name: value[0] for name, value in schema.items()}, types
seen, buckets = set(), set()
for line in text.splitlines():
    if line.startswith("#") or not line:
        continue
    match = re.fullmatch(r"(\w+)(?:\{(.*)\})? ([^ ]+)", line)
    assert match, line
    name, labels, value = match.groups()
    float(value)
    label_pairs = re.findall(r'(\w+)="((?:\\.|[^"\\])*)"', labels or "")
    label_names = {key for key, _ in label_pairs}
    family = name
    if name.startswith("rpc_xray_request_duration_seconds_"):
        family = "rpc_xray_request_duration_seconds"
        if name.endswith("_bucket"):
            buckets.add(dict(label_pairs)["le"])
            label_names.remove("le")
    assert family in schema, name
    assert label_names == set(schema[family][1]), (name, label_names)
    key = (name, tuple(sorted(label_pairs)))
    assert key not in seen, f"duplicate sample: {key}"
    seen.add(key)
assert buckets == {str(v) for v in contract["buckets"]} | {"+Inf"}, buckets
assert {key[0] for key in seen} >= set(schema) - {"rpc_xray_request_duration_seconds"}
print(
    f"{sys.argv[1]}: all 12 metric families, labels, buckets, and unique samples match"
)
