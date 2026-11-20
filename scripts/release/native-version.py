import re
import sys
import tomllib
from pathlib import Path

sdk, expected = sys.argv[1:]
if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", expected):
    sys.exit("Enter a stable package version without a v prefix")

if sdk == "python":
    actual = tomllib.loads(Path("python/pyproject.toml").read_text())["project"]["version"]
elif sdk == "rust":
    actual = tomllib.loads(Path("rust/Cargo.toml").read_text())["package"]["version"]
elif sdk == "go":
    actual = re.search(r'^const Version = "([^"]+)"$', Path("go/xray.go").read_text(), re.M)[1]
else:
    sys.exit("Unknown SDK")
if actual != expected:
    sys.exit(f"Expected {sdk} version {actual}, got {expected}")
