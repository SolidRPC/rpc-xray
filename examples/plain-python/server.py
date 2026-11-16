import asyncio
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
from rpc_xray import CONTENT_TYPE, AsyncTransport, Xray


class App:
    def __init__(self, rpc_url, chain, host="127.0.0.1", port=9464, interval=1.0):
        parsed = urlsplit(rpc_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or not chain:
            raise ValueError(
                "Set RPC_URL to an HTTP endpoint and CHAIN to its chain label"
            )
        if interval < 1:
            raise ValueError("Polling interval must be at least one second")
        self.telemetry = Xray()
        self.stop = threading.Event()
        self.state = {"chain": chain, "status": "starting", "blockNumber": None}
        self.lock = threading.Lock()
        app = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/metrics":
                    body, content_type, status = (
                        app.telemetry.metrics().encode(),
                        CONTENT_TYPE,
                        200,
                    )
                elif self.path == "/":
                    with app.lock:
                        body = json.dumps(app.state).encode()
                    content_type, status = "application/json", 200
                else:
                    body, content_type, status = b"Not found", "text/plain", 404
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.http_thread = threading.Thread(target=self.server.serve_forever)

        async def poll():
            async with httpx.AsyncClient(
                transport=AsyncTransport(self.telemetry, chain=chain), timeout=4.5
            ) as client:
                request_id = 0
                while not self.stop.is_set():
                    started = time.monotonic()
                    request_id += 1
                    try:
                        response = await asyncio.wait_for(
                            client.post(
                                rpc_url,
                                json={
                                    "jsonrpc": "2.0",
                                    "id": request_id,
                                    "method": "eth_blockNumber",
                                    "params": [],
                                },
                            ),
                            timeout=4.5,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        result = payload.get("result")
                        if (
                            payload.get("error") is not None
                            or not isinstance(result, str)
                            or not result.startswith("0x")
                        ):
                            raise ValueError("Invalid block result")
                        int(result, 16)
                        with self.lock:
                            self.state.update(status="ok", blockNumber=result)
                    except Exception:
                        with self.lock:
                            self.state["status"] = "error"
                    await asyncio.sleep(max(0, interval - (time.monotonic() - started)))

        self.loop = asyncio.new_event_loop()
        self.poll_task = self.loop.create_task(poll())

        def run_polling():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(self.poll_task)
            except asyncio.CancelledError:
                pass
            finally:
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.close()

        self.poll_thread = threading.Thread(target=run_polling)
        self.http_thread.start()
        self.poll_thread.start()

    def close(self):
        self.stop.set()
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.poll_task.cancel)
        self.poll_thread.join()
        self.server.shutdown()
        self.server.server_close()
        self.http_thread.join()


def main():
    app = None
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        app = App(
            os.environ.get("RPC_URL", ""),
            os.environ.get("CHAIN", ""),
            os.environ.get("HOST", "127.0.0.1"),
            int(os.environ.get("PORT", "9464")),
        )
        print(
            f"RPC example listening on port {app.port}. Metrics at /metrics. Press Ctrl+C to stop.",
            flush=True,
        )
        stopped.wait()
    except Exception:
        print("Could not start. Check RPC_URL, CHAIN, HOST, and PORT.")
        raise SystemExit(1)
    finally:
        if app:
            app.close()


if __name__ == "__main__":
    main()
