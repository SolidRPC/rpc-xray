import asyncio
import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from rpc_xray import AsyncTransport, Transport, Xray, chain_label
from rpc_xray.core import classify, parse_request

ROOT = Path(__file__).resolve().parents[2]
REQUEST = b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
OK = b'{"jsonrpc":"2.0","id":1,"result":"0x10"}'


@pytest.mark.parametrize(
    "vector",
    json.loads((ROOT / "spec/vectors.json").read_text()),
    ids=lambda v: v["name"],
)
def test_shared_vectors(vector):
    assert (
        classify(
            parse_request(vector["request"]),
            vector.get("status", 200),
            vector.get("response"),
            vector.get("failure"),
        )
        == vector["expected"]
    )


def samples(xray):
    return [
        sample
        for family in text_string_to_metric_families(xray.metrics())
        for sample in family.samples
    ]


def test_passthrough_batch_metrics_and_privacy():
    seen = []
    body = b'[{"id":2,"error":{"code":3,"message":"revert"}},{"id":1,"result":null}]'
    batch = b'[{"id":1,"method":"eth_blockNumber"},{"id":2,"method":"eth_call"}]'

    def handle(request):
        seen.append((request.content, request.headers["x-fixture"], str(request.url)))
        return httpx.Response(200, content=body, headers={"x-response": "unchanged"})

    xray = Xray()
    with httpx.Client(
        transport=Transport(xray, chain="base", transport=httpx.MockTransport(handle))
    ) as client:
        response = client.post(
            "https://name:secret@RPC.EXAMPLE:8545/private?key=secret",
            content=batch,
            headers={"x-fixture": "test"},
        )
        assert (
            response.content == body and response.headers["x-response"] == "unchanged"
        )
    assert len(seen) == 1 and seen[0][0] == batch and seen[0][1] == "test"
    metrics = xray.metrics()
    assert all(value not in metrics for value in ["secret", "private", "name:", "key="])
    requests = [s for s in samples(xray) if s.name == "rpc_xray_requests_total"]
    assert {(s.labels["method"], s.labels["outcome"], s.value) for s in requests} == {
        ("eth_blockNumber", "ok", 1),
        ("eth_call", "caller_error", 1),
    }
    counts = [s for s in samples(xray) if s.name.endswith("duration_seconds_count")]
    assert (
        len(counts) == 1
        and counts[0].labels["method"] == "batch"
        and counts[0].value == 1
    )
    assert 'provider="rpc.example:8545"' in metrics


@pytest.mark.parametrize(
    "exception,outcome",
    [
        (httpx.ReadTimeout("timeout"), "timeout"),
        (httpx.ConnectError("offline"), "network"),
    ],
)
def test_preserves_exception(exception, outcome):
    def fail(_):
        raise exception

    xray = Xray()
    with httpx.Client(
        transport=Transport(xray, transport=httpx.MockTransport(fail))
    ) as client:
        with pytest.raises(type(exception)) as result:
            client.post("https://rpc.example", content=REQUEST)
    assert result.value is exception
    assert f'outcome="{outcome}"' in xray.metrics()


def test_stream_cap_close_and_no_read_ahead():
    reads = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in [b"x" * 80, b"y" * 80]:
                reads.append(1)
                yield chunk

    xray = Xray(max_body_bytes=100)
    with httpx.Client(
        transport=Transport(
            xray,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, stream=Stream())
            ),
        )
    ) as client:
        with client.stream("POST", "https://rpc.example", content=REQUEST) as response:
            assert not reads
            assert response.read() == b"x" * 80 + b"y" * 80
    assert len(reads) == 2
    assert 'outcome="ok"} 1' in xray.metrics()
    assert 'outcome="rpc_error"' not in xray.metrics()


async def test_async_and_cancellation():
    xray = Xray()

    async def handle(_):
        return httpx.Response(200, content=OK)

    async with httpx.AsyncClient(
        transport=AsyncTransport(xray, transport=httpx.MockTransport(handle))
    ) as client:
        assert (await client.post("https://rpc.example", content=REQUEST)).content == OK
    assert 'outcome="ok"} 1' in xray.metrics()
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=AsyncTransport(xray, transport=httpx.MockTransport(blocked))
    ) as client:
        task = asyncio.create_task(client.post("https://rpc.example", content=REQUEST))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert 'outcome="timeout"} 1' in xray.metrics()


def test_fail_open_non_rpc_and_streaming_request():
    xray = Xray()
    xray.start = lambda *_: (_ for _ in ()).throw(RuntimeError("telemetry failure"))
    seen = []

    def handle(request):
        seen.append(request.read())
        return httpx.Response(200, content=OK)

    with httpx.Client(
        transport=Transport(xray, transport=httpx.MockTransport(handle))
    ) as client:
        assert client.post("https://rpc.example", content=REQUEST).content == OK
        assert client.post("https://rpc.example", content=iter([REQUEST])).content == OK
    assert seen == [REQUEST, REQUEST]
    assert "rpc_xray_requests_total" not in xray.metrics()


def test_probe_metrics_concurrency_and_limits():
    xray = Xray(value_per_request=0.1, revenue_per_hour=100)

    def record():
        for _ in range(50):
            o = xray.start("https://rpc.example/secret", "ethereum", REQUEST)
            o.finish(200, OK)
            o.finish(500, b"")
            list(samples(xray))

    threads = [threading.Thread(target=record) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert 'outcome="ok"} 200' in xray.metrics()
    xray.record_probe("https://rpc.example/secret", "ethereum", 10)
    xray.record_probe("https://second.example", "ethereum", 12)
    xray.record_probe("https://rpc.example/secret", "ethereum", None)
    values = samples(xray)
    lag = next(
        s
        for s in values
        if s.name == "rpc_xray_head_lag_blocks"
        and s.labels["provider"] == "rpc.example"
    )
    assert lag.value == 2
    assert len([s for s in values if s.name.endswith("duration_seconds_bucket")]) == 10
    for i in range(40):
        xray.start(f"https://rpc{i}.example", "ethereum", REQUEST).finish(200, OK)
    assert len(xray._endpoints) == 32
    assert 'provider="other"' in xray.metrics()
    assert chain_label(1) == "ethereum" and chain_label(123456) == "eip155:123456"


def test_plain_example_local_only():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    calls = []

    class Mock(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(
                json.loads(self.rfile.read(int(self.headers["content-length"])))
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(OK)))
            self.end_headers()
            self.wfile.write(OK)

        def log_message(self, *_):
            pass

    mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
    thread = threading.Thread(target=mock.serve_forever)
    thread.start()
    spec = importlib.util.spec_from_file_location(
        "plain_python", ROOT / "examples/plain-python/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = module.App(f"http://127.0.0.1:{mock.server_port}/secret", "ethereum", port=0)
    try:
        with httpx.Client() as client:
            for _ in range(100):
                if client.get(f"http://127.0.0.1:{app.port}/").json()["status"] == "ok":
                    break
                time.sleep(0.01)
            else:
                pytest.fail("example did not poll")
            for _ in range(5):
                response = client.get(f"http://127.0.0.1:{app.port}/metrics")
                assert (
                    "rpc_xray_requests_total" in response.text
                    and "secret" not in response.text
                )
            assert [call["method"] for call in calls] == ["eth_blockNumber"]
    finally:
        app.close()
        mock.shutdown()
        mock.server_close()
        thread.join()
    count = len(calls)
    time.sleep(1.05)
    assert len(calls) == count


def test_metric_contract():
    xray = Xray(value_per_request=0.1, revenue_per_hour=100)
    observation = xray.start("https://rpc.example", "ethereum", REQUEST)
    observation.started -= 1.5
    observation.finish(200, OK)
    xray.record_probe("https://rpc.example", "ethereum", 10)
    if path := os.environ.get("RPC_XRAY_METRICS_FIXTURE"):
        Path(path).write_text(xray.metrics())
    assert len(list(text_string_to_metric_families(xray.metrics()))) == 12
