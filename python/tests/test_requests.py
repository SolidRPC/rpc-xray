import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from rpc_xray import Xray
from rpc_xray.requests import instrument_requests
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3RPCError


@pytest.fixture(params=["identity", "gzip"])
def rpc_server(request):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append({**payload, "_headers": dict(self.headers)})
            response = {"jsonrpc": "2.0", "id": payload["id"], "result": "0x10"}
            if payload["method"] == "eth_call":
                response.pop("result")
                response["error"] = {"code": 3, "message": "execution reverted"}
            if self.path.endswith("/provider-error"):
                response.pop("result", None)
                response["error"] = {"code": -32603, "message": "internal error"}
            if self.path.endswith("/large"):
                response.pop("result", None)
                response["error"] = {"code": -32603, "message": "x" * 2048}
            body = json.dumps(response).encode()
            if self.path.endswith("/malformed"):
                body = b"not JSON"
            if request.param == "gzip":
                body = gzip.compress(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if self.path.endswith("/unsupported"):
                self.send_header("Content-Encoding", "x-fixture")
            elif request.param == "gzip":
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture-secret", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web3_http_provider(rpc_server):
    url, calls = rpc_server
    telemetry = Xray()
    with instrument_requests(
        requests.Session(), telemetry, chain="ethereum"
    ) as session:
        web3 = Web3(
            HTTPProvider(url, session=session, exception_retry_configuration=None)
        )
        assert web3.eth.block_number == 16
        assert web3.provider.make_request("eth_call", [])["error"]["code"] == 3
    assert [call["method"] for call in calls] == ["eth_blockNumber", "eth_call"]
    metrics = telemetry.metrics()
    assert 'outcome="ok"} 1' in metrics
    assert 'outcome="caller_error"} 1' in metrics
    assert "fixture-secret" not in metrics


@pytest.mark.parametrize(
    "method,outcome", [("eth_blockNumber", "ok"), ("eth_call", "caller_error")]
)
def test_requests_stream_preserved(rpc_server, method, outcome):
    url, calls = rpc_server
    telemetry = Xray()
    with instrument_requests(
        requests.Session(), telemetry, chain="ethereum"
    ) as session:
        session.headers["X-Fixture"] = "unchanged"
        response = session.post(url, json={"id": 1, "method": method}, stream=True)
        assert "rpc_xray_requests_total" not in telemetry.metrics()
        body = b"".join(response.iter_content(chunk_size=7))
        response.close()
    payload = json.loads(body)
    if method == "eth_blockNumber":
        assert payload["result"] == "0x10"
    else:
        assert payload["error"]["code"] == 3
    assert f'outcome="{outcome}"}} 1' in telemetry.metrics()
    assert len(calls) == 1
    assert calls[0]["_headers"]["X-Fixture"] == "unchanged"
    assert "gzip" in calls[0]["_headers"]["Accept-Encoding"]


def test_web3_error_preserved(rpc_server):
    url, calls = rpc_server
    telemetry = Xray()
    with instrument_requests(
        requests.Session(), telemetry, chain="ethereum"
    ) as session:
        web3 = Web3(
            HTTPProvider(
                url + "/provider-error",
                session=session,
                exception_retry_configuration=None,
            )
        )
        with pytest.raises(Web3RPCError) as raised:
            _ = web3.eth.block_number
    assert raised.value.rpc_response["error"] == {
        "code": -32603,
        "message": "internal error",
    }
    assert 'outcome="rpc_error"} 1' in telemetry.metrics()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "method,outcome", [("eth_blockNumber", "ok"), ("eth_call", "caller_error")]
)
def test_requests_decoded_raw_read(rpc_server, method, outcome):
    url, calls = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url, json={"id": 1, "method": method}, stream=True
        ) as response:
            assert response.raw.read(0, decode_content=True) == b""
            assert "rpc_xray_requests_total" not in telemetry.metrics()
            body = response.raw.read(None, True)
    payload = json.loads(body)
    if method == "eth_blockNumber":
        assert payload["result"] == "0x10"
    else:
        assert payload["error"]["code"] == 3
    assert f'outcome="{outcome}"}} 1' in telemetry.metrics()
    assert len(calls) == 1


def test_requests_raw_decode_attribute_preserved(rpc_server):
    url, _ = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url, json={"id": 1, "method": "eth_call"}, stream=True
        ) as response:
            response.raw.decode_content = True
            body = response.raw.read()
    assert json.loads(body)["error"]["code"] == 3
    assert 'outcome="caller_error"} 1' in telemetry.metrics()


@pytest.mark.parametrize("read_mode", ["read", "stream"])
def test_requests_raw_undecoded_preserved(rpc_server, read_mode):
    url, _ = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url, json={"id": 1, "method": "eth_call"}, stream=True
        ) as response:
            encoded = response.headers.get("Content-Encoding") == "gzip"
            body = (
                response.raw.read()
                if read_mode == "read"
                else b"".join(response.raw.stream(7, False))
            )
    assert json.loads(gzip.decompress(body) if encoded else body)["error"]["code"] == 3
    expected = "ok" if encoded else "caller_error"
    assert f'outcome="{expected}"}} 1' in telemetry.metrics()


def test_requests_partial_close_uses_status(rpc_server):
    url, _ = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url, json={"id": 1, "method": "eth_call"}, stream=True
        ) as response:
            assert response.raw.read(5, decode_content=True) == b'{"jso'
            assert "rpc_xray_requests_total" not in telemetry.metrics()
    assert 'outcome="ok"} 1' in telemetry.metrics()
    assert 'outcome="rpc_error"' not in telemetry.metrics()


def test_requests_decoded_body_cap(rpc_server):
    url, _ = rpc_server
    telemetry = Xray(max_body_bytes=256)
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url + "/large", json={"id": 1, "method": "eth_call"}, stream=True
        ) as response:
            if response.headers.get("Content-Encoding") == "gzip":
                assert int(response.headers["Content-Length"]) < 256
            body = b"".join(response.iter_content(chunk_size=31))
            if response.headers.get("Content-Encoding") == "gzip":
                assert len(response.raw.capture.body) <= 256
    assert json.loads(body)["error"]["message"] == "x" * 2048
    assert 'outcome="ok"} 1' in telemetry.metrics()


def test_requests_encoded_length_does_not_limit_decoded_body(rpc_server):
    url, _ = rpc_server
    telemetry = Xray(max_body_bytes=88)
    with instrument_requests(requests.Session(), telemetry) as session:
        response = session.post(url, json={"id": 1, "method": "eth_call"})
    if response.headers.get("Content-Encoding") == "gzip":
        assert int(response.headers["Content-Length"]) > 88
    assert len(response.content) == 82
    assert response.json()["error"]["code"] == 3
    assert 'outcome="caller_error"} 1' in telemetry.metrics()


def test_requests_capture_failure_preserves_body(rpc_server, monkeypatch):
    from rpc_xray.httpx import _Capture

    def fail(*_):
        raise RuntimeError("telemetry failure")

    monkeypatch.setattr(_Capture, "feed", fail)
    url, calls = rpc_server
    with instrument_requests(requests.Session(), Xray()) as session:
        response = session.post(url, json={"id": 1, "method": "eth_call"})
    assert response.json()["error"]["code"] == 3
    assert len(calls) == 1


def test_requests_malformed_decoded_body(rpc_server):
    url, _ = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        response = session.post(
            url + "/malformed", json={"id": 1, "method": "eth_call"}
        )
        assert response.content == b"not JSON"
        with pytest.raises(requests.exceptions.JSONDecodeError):
            response.json()
    assert 'outcome="rpc_error"} 1' in telemetry.metrics()


def test_requests_unsupported_encoding_uses_status(rpc_server):
    url, _ = rpc_server
    telemetry = Xray()
    with instrument_requests(requests.Session(), telemetry) as session:
        with session.post(
            url + "/unsupported", json={"id": 1, "method": "eth_call"}
        ) as response:
            assert response.status_code == 200
            assert response.content
    assert 'outcome="ok"} 1' in telemetry.metrics()
