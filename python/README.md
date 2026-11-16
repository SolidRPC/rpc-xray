# RPC X-Ray for Python

Prometheus metrics for HTTPX sync and async clients, requests, and web3.py's synchronous HTTPProvider. Python 3.10 or newer.

## Install

The package name is `solidrpc-rpc-xray` and the import is `rpc_xray`. This SDK is not published yet. After its first PyPI release:

```bash
pip install solidrpc-rpc-xray
```

Before publication, build and install the distributable wheel from the repository root. This example includes the requests adapter and web3.py:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install build
.venv/bin/python -m build python
.venv/bin/python -m pip install 'python/dist/solidrpc_rpc_xray-0.1.0-py3-none-any.whl[requests]' 'web3>=7,<9'
```

For an HTTPX-only app, install the same wheel without `[requests]` or `web3`. Existing applications can install the wheel into their own environment and retain their supported web3.py version. The wheel installs the package, not an editable checkout. Building it does not publish anything. Contributor setup is covered under [test and package](#test-and-package).

## Integrate

```python
import httpx
from rpc_xray import Xray, Transport

telemetry = Xray()
transport = Transport(telemetry, chain="ethereum", transport=httpx.HTTPTransport())
client = httpx.Client(transport=transport, timeout=4.5)
```

Keep your existing client configuration, request URLs, headers, retry policy, and timeouts. For async clients use `AsyncTransport` with your `httpx.AsyncHTTPTransport` and pass it to `httpx.AsyncClient`. Close clients normally, including `await client.aclose()` for async clients.

Expose `telemetry.metrics()` from the same instance at `GET /metrics` with `Content-Type: text/plain; version=0.0.4; charset=utf-8`. The [complete example](../examples/plain-python/README.md) includes a server, polling, shutdown, and Prometheus configuration.

The adapters observe buffered POST requests. Streaming request bodies are skipped without reading them. Response bytes are copied as the application consumes the body, up to 1 MiB by default. Complete inspected bodies receive JSON-RPC classification. Oversized or prematurely closed responses use HTTP status only. The adapter never reads ahead, retries, or starts RPC calls. Metrics appear when the body is consumed or closed, so close streamed responses even when you do not read them.

Both HTTPX adapters and the requests adapter observe bytes already decoded for the caller. The body cap applies to decoded bytes, so a small compressed response cannot bypass the inspection limit by expanding into a large body. Instrumentation does not change Accept-Encoding or perform a second decompression.

HTTPX compression works with normal buffered responses and decoded streaming methods, including `read()` / `aread()`, `iter_bytes()` / `aiter_bytes()`, and text or line iteration. The adapter decorates the response's decoded byte iterator and close method while preserving the response object, headers, bytes, and original exceptions. It waits until decoded iteration completes, including the final buffered chunks after the network stream closes, before classifying JSON-RPC content.

Gzip and deflate use HTTPX's built-in decoders. Brotli and Zstandard work when supported by the installed HTTPX version and its optional decoder dependencies. See [HTTPX response decoding](https://www.python-httpx.org/quickstart/#binary-response-content) for installation details. RPC X-Ray adds no runtime decoder dependency. An HTTPX decoding failure during inspection counts as `rpc_error` and still raises the original `httpx.DecodingError` to the application.

Raw `iter_raw()` / `aiter_raw()` reads retain their original wire bytes. Compressed raw reads, unread or partially consumed responses, and bodies exceeding the decoded cap use HTTP status alone. Repeated reads and closes do not count a request again. These boundaries apply equally to sync and async clients.

## requests and web3.py

Install the `requests` extra with `pip install 'solidrpc-rpc-xray[requests]'` after publication. Before publication, use the wheel installation recipe above. Install `web3` separately when using its HTTPProvider.

```python
import requests
from web3 import HTTPProvider, Web3
from rpc_xray import Xray
from rpc_xray.requests import instrument_requests

telemetry = Xray()
session = instrument_requests(requests.Session(), telemetry, chain="ethereum")
provider = HTTPProvider(url, session=session, request_kwargs={"timeout": 4.5}, exception_retry_configuration=None)
w3 = Web3(provider)
```

Here `url` is your existing endpoint. Use the application's existing timeout and retry configuration when integrating. `instrument_requests` wraps each existing session adapter and preserves its configuration. An adapter call includes retries performed inside that adapter, so place instrumentation at the level you intend to measure. Close the session when finished. Normal requests body consumption and `iter_content()` are observed, while direct lower-level raw methods other than `read()` and `stream()` are outside the adapter's inspection scope.

Gzip success and JSON-RPC error responses are classified when requests consumes them through `.content`, `.json()`, or `.iter_content()`. Raw `.read()` and `.stream()` calls are inspected when their bytes are unencoded or decoding is enabled with `decode_content=True` or `response.raw.decode_content = True`, and the underlying client supports that content encoding. Reading compressed raw bytes without decoding uses HTTP status only. The adapter never changes Accept-Encoding or performs its own decompression.

The web3.py integration supports synchronous HTTPProvider on web3.py 7 and 8. AsyncHTTPProvider uses aiohttp and is not wrapped by this release. HTTPX async clients are supported through `AsyncTransport`.

For other clients, `telemetry.start(url, chain, request_bytes)` returns an optional observation. Call `observation.finish(status, response_bytes)` once after the client's existing body read, or `observation.finish(failure="timeout")` / `failure="network"`. Preserve and re-raise the original exception.

## Metrics and optional probes

The [shared specification](../spec/README.md) defines all names, labels, histogram buckets, and error classifications. A batch counts each RPC call and one HTTP duration. Provider labels contain only the host and any non-default port, never credentials, paths, or query strings. Custom method names fold into `other`. Endpoints and metric series are bounded.

The SDK is passive. It does not detect chains, schedule head probes, or print offers. Supply `chain` explicitly or get `unknown`. `rpc_xray_offers_enabled` is always `0`. Optional `value_per_request` and `revenue_per_hour` constructor values enable the corresponding USD gauges.

If your application deliberately runs a separate head probe, record its result with `telemetry.record_probe(url, chain, head)` or `head=None` for failure. Use an unwrapped client for that probe so it is counted only in `rpc_xray_probe_requests_total`. This enables the four head gauges. Without probe observations those gauges are absent, not zero. Scraping metrics never sends a probe.

## Test and package

For SDK contributors, install the editable package and test dependencies from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e './python[test,requests]'
.venv/bin/python -m pytest python/tests
.venv/bin/python -m build python
.venv/bin/python scripts/test-example.py python --gzip
```

Tests use shared classification vectors, real local HTTP fixtures, and mock transports. The gzip example check runs the complete async app, verifies error counters and recovery through `/metrics`, and stops it afterward. CI runs the compression tests against both the minimum supported HTTPX version and the latest compatible version, including a freshly installed wheel. Building the wheel and source distribution does not publish them.
