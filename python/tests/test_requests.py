import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests
from web3 import HTTPProvider, Web3

from rpc_xray import Xray
from rpc_xray.requests import instrument_requests


@pytest.fixture
def rpc_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append(payload)
            response = {"jsonrpc": "2.0", "id": payload["id"], "result": "0x10"}
            if payload["method"] == "eth_call":
                response.pop("result")
                response["error"] = {"code": 3, "message": "execution reverted"}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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


def test_requests_stream_preserved(rpc_server):
    url, calls = rpc_server
    telemetry = Xray()
    with instrument_requests(
        requests.Session(), telemetry, chain="ethereum"
    ) as session:
        response = session.post(url, json={"id": 1, "method": "eth_call"}, stream=True)
        assert "rpc_xray_requests_total" not in telemetry.metrics()
        body = b"".join(response.iter_content(chunk_size=7))
        response.close()
    assert json.loads(body)["error"]["code"] == 3
    assert 'outcome="caller_error"} 1' in telemetry.metrics()
    assert len(calls) == 1
