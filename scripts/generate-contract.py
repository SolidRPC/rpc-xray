import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
contract = json.loads((ROOT / "spec/contract.json").read_text())
names = {
    "methods": "METHODS",
    "chains": "CHAINS",
    "buckets": "BUCKETS",
    "callerCodes": "CALLER_CODES",
    "ratePatterns": "RATE_PATTERNS",
    "callerPatterns": "CALLER_PATTERNS",
}
(ROOT / "python/rpc_xray/_contract.py").write_text(
    "\n".join(f"{names[key]} = {value!r}" for key, value in contract.items()) + "\n"
)
(ROOT / "go/contract.go").write_text(
    "package rpcxray\n\nconst contractJSON = `"
    + json.dumps(contract, separators=(",", ":"))
    + "`\n"
)
(ROOT / "rust/src/contract.json").write_bytes(
    (ROOT / "spec/contract.json").read_bytes()
)
for target in ("go/testdata/vectors.json", "rust/src/vectors.json"):
    path = ROOT / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((ROOT / "spec/vectors.json").read_bytes())
js = []
for key, value in contract.items():
    encoded = json.dumps(value, indent=2)
    if key in {"methods", "callerCodes"}:
        encoded = "new Set(" + encoded + ")"
    annotation = ": Record<string, string>" if key == "chains" else ""
    js.append(f"export const {names[key]}{annotation} = {encoded};")
(ROOT / "js/src/contract.ts").write_text("\n\n".join(js) + "\n")
