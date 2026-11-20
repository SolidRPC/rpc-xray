import argparse
import gzip
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("language", choices=["python", "go", "rust", "go-ethereum", "alloy"])
parser.add_argument("--gzip", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
calls = []
lock = threading.Lock()


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
        with lock:
            calls.append((time.monotonic(), payload))
            number = len(calls)
        response = {"jsonrpc": "2.0", "id": payload["id"]}
        if number == 1:
            response["error"] = {
                "code": 3,
                "message": "execution reverted: fixture-secret",
            }
        else:
            response["result"] = "0x10"
        body = json.dumps(response).encode()
        if args.gzip:
            body = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if args.gzip:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def get(port, path):
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=1
    ) as response:
        return response.headers, response.read().decode()


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.03)
    raise AssertionError("example did not reach expected state")


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
mock = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
thread = threading.Thread(target=mock.serve_forever)
thread.start()
command = {
    "python": [sys.executable, "server.py"],
    "go": [str(root / "tmp/rpc-xray-go")],
    "rust": [str(root / "examples/plain-rust/target/debug/rpc-xray-example")],
    "go-ethereum": [str(root / "tmp/rpc-xray-go-ethereum")],
    "alloy": [str(root / "examples/alloy/target/debug/rpc-xray-alloy-example")],
}[args.language]
example_directory = {
    "go-ethereum": "go-ethereum",
    "alloy": "alloy",
}.get(args.language, f"plain-{args.language}")
env = {
    **os.environ,
    "RPC_URL": f"http://127.0.0.1:{mock.server_port}/fixture-secret?key=fixture-secret",
    "CHAIN": "ethereum",
    "HOST": "127.0.0.1",
    "PORT": str(port),
}
process = None
try:
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            command,
            cwd=root / "examples" / example_directory,
            env=env,
            stdout=log,
            stderr=log,
        )
        wait_for(lambda: 'outcome="caller_error"' in get(port, "/metrics")[1])
        wait_for(lambda: json.loads(get(port, "/")[1])["status"] == "ok")
        headers, metrics = get(port, "/metrics")
        assert headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert "fixture-secret" not in metrics
        assert 'outcome="ok"' in metrics
        assert 'rpc_xray_head_block{chain="ethereum",provider="127.0.0.1:' in metrics
        assert "rpc_xray_probe_requests_total" not in metrics
        assert "rpc_xray_offers_enabled 1" in metrics
        with lock:
            snapshot = list(calls)
        assert all(call["method"] == "eth_blockNumber" for _, call in snapshot)
        assert all(
            right[0] - left[0] > 0.85 for left, right in zip(snapshot, snapshot[1:])
        )
        for _ in range(5):
            get(port, "/metrics")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=8) == 0
        with lock:
            count = len(calls)
        time.sleep(1.1)
        assert len(calls) == count
        log.seek(0)
        output = log.read()
        assert "fixture-secret" not in output
        print(
            f"{args.language}: metrics, caller errors, recovery, pacing, privacy, and shutdown passed ({count} local RPC calls)"
        )
finally:
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    mock.shutdown()
    mock.server_close()
    thread.join()
