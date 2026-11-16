import asyncio
from contextlib import asynccontextmanager
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import unquote, urlsplit
import zlib

import brotli
import httpx
import pytest
import zstandard
from prometheus_client.parser import text_string_to_metric_families

from rpc_xray import AsyncTransport, Transport, Xray


REQUEST = b'{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
PAYLOADS = {
    "ok": b'{"jsonrpc":"2.0","id":1,"result":"0x10"}',
    "caller_error": b'{"jsonrpc":"2.0","id":1,"error":{"code":3,"message":"revert fixture-secret"}}',
    "rpc_error": b'{"jsonrpc":"2.0","id":1,"error":{"code":-32603,"message":"fixture-secret"}}',
    "malformed": b"not json",
}
with httpx.Client() as probe:
    HAS_ZSTD = "zstd" in probe.headers["accept-encoding"]
ENCODINGS = [
    "gzip",
    "deflate",
    "raw-deflate",
    "br",
    pytest.param(
        "zstd", marks=pytest.mark.skipif(not HAS_ZSTD, reason="HTTPX lacks zstd")
    ),
    "gzip, deflate",
]


def encode(body, encoding):
    if encoding == "gzip":
        return gzip.compress(body), encoding
    if encoding == "deflate":
        return zlib.compress(body), encoding
    if encoding == "raw-deflate":
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        return compressor.compress(body) + compressor.flush(), "deflate"
    if encoding == "br":
        return brotli.compress(body), encoding
    if encoding == "zstd":
        return zstandard.ZstdCompressor().compress(body), encoding
    if encoding == "gzip, deflate":
        return zlib.compress(gzip.compress(body)), encoding
    return body, encoding


def outcomes(xray):
    return {
        sample.labels["outcome"]: sample.value
        for family in text_string_to_metric_families(xray.metrics())
        for sample in family.samples
        if sample.name == "rpc_xray_requests_total"
    }


@asynccontextmanager
async def client_for(mode, xray=None, transport=None):
    if mode == "sync":
        if xray is not None:
            transport = Transport(xray, chain="ethereum", transport=transport)
        with httpx.Client(transport=transport, timeout=1, trust_env=False) as client:
            yield client
    else:
        if xray is not None:
            transport = AsyncTransport(xray, chain="ethereum", transport=transport)
        async with httpx.AsyncClient(
            transport=transport, timeout=1, trust_env=False
        ) as client:
            yield client


async def post(client, mode, url, content=REQUEST):
    result = client.post(url, content=content, headers={"x-fixture": "unchanged"})
    return result if mode == "sync" else await result


@pytest.fixture(scope="module")
def server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(
                (self.rfile.read(int(self.headers["content-length"])), self.headers)
            )
            encoding, case = unquote(urlsplit(self.path).path).strip("/").split("/")
            body, encoding = encode(PAYLOADS[case], encoding)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Response", "unchanged")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("case", ["ok", "caller_error", "rpc_error", "malformed"])
async def test_real_compressed_responses(mode, encoding, case, server):
    base, calls = server
    url = f"{base}/{encoding}/{case}?key=fixture-secret"
    async with client_for(mode) as plain:
        original = await post(plain, mode, url)
    xray = Xray()
    before = len(calls)
    async with client_for(mode, xray) as wrapped:
        response = await post(wrapped, mode, url)
        assert response.content == original.content == PAYLOADS[case]
        assert response.status_code == original.status_code
        assert (
            response.headers["content-encoding"] == original.headers["content-encoding"]
        )
        assert response.headers["content-length"] == original.headers["content-length"]
        assert response.headers["x-response"] == "unchanged"
        if mode == "sync":
            assert response.read() == original.content
            response.close()
        else:
            assert await response.aread() == original.content
            await response.aclose()
    expected = "rpc_error" if case == "malformed" else case
    assert outcomes(xray) == {expected: 1}
    for _ in range(10):
        assert "fixture-secret" not in xray.metrics()
    assert len(calls) == before + 1
    assert calls[-1][0] == REQUEST
    assert calls[-1][1]["x-fixture"] == "unchanged"


class SyncStream(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error
        self.reads = 0
        self.closed = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error:
            raise self.error

    def close(self):
        self.closed += 1


class AsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error
        self.reads = 0
        self.closed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed += 1


def mock(mode, wire, encoding="gzip", status=200, error=None, chunk_size=5):
    chunks = [wire[i : i + chunk_size] for i in range(0, len(wire), chunk_size)]
    stream = (SyncStream if mode == "sync" else AsyncStream)(chunks, error)
    response = httpx.Response(
        status,
        stream=stream,
        headers={"Content-Encoding": encoding, "Content-Length": str(len(wire))},
    )
    return httpx.MockTransport(lambda _: response), stream, response


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("consumption", ["bytes", "text", "raw", "partial", "unread"])
async def test_streaming_preserves_chunks_no_read_ahead_and_close(mode, consumption):
    wire = gzip.compress(PAYLOADS["rpc_error"])
    transport, stream, original = mock(mode, wire)
    xray = Xray()
    async with client_for(mode, xray, transport) as client:
        request = client.build_request("POST", "https://rpc.example", content=REQUEST)
        response = client.send(request, stream=True)
        if mode == "async":
            response = await response
        assert response is original
        assert stream.reads == 0
        chunks = []
        if consumption != "unread":
            method = (
                "iter_raw"
                if consumption == "raw"
                else "iter_text"
                if consumption == "text"
                else "iter_bytes"
            )
            iterator = getattr(response, ("a" if mode == "async" else "") + method)(
                chunk_size=7
            )
            if mode == "sync":
                for chunk in iterator:
                    chunks.append(chunk)
                    if consumption == "partial":
                        break
            else:
                async for chunk in iterator:
                    chunks.append(chunk)
                    if consumption == "partial":
                        break
        if mode == "sync":
            response.close()
        else:
            await response.aclose()
        if consumption in ("raw", "bytes", "text"):
            observed = (
                "".join(chunks).encode() if consumption == "text" else b"".join(chunks)
            )
            assert observed == (wire if consumption == "raw" else PAYLOADS["rpc_error"])
        if consumption == "unread":
            assert stream.reads == 0
        expected = "rpc_error" if consumption in ("bytes", "text") else "ok"
        assert outcomes(xray) == {expected: 1}
        if consumption == "partial":
            if mode == "sync":
                iterator.close()
            else:
                await iterator.aclose()
        assert outcomes(xray) == {expected: 1}
    assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_chunker_flush_after_raw_close_is_inspected(mode):
    transport, stream, _ = mock(mode, gzip.compress(PAYLOADS["caller_error"]))
    xray = Xray()
    async with client_for(mode, xray, transport) as client:
        response = client.send(
            client.build_request("POST", "https://rpc.example", content=REQUEST),
            stream=True,
        )
        if mode == "sync":
            body = b"".join(response.iter_bytes(chunk_size=1024))
        else:
            response = await response
            body = b"".join(
                [chunk async for chunk in response.aiter_bytes(chunk_size=1024)]
            )
        assert body == PAYLOADS["caller_error"]
        assert stream.closed == 1
        assert outcomes(xray) == {"caller_error": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_explicit_close_finishes_a_held_decoded_iterator(mode):
    transport, stream, _ = mock(mode, gzip.compress(PAYLOADS["rpc_error"]))
    xray = Xray()
    async with client_for(mode, xray, transport) as client:
        response = client.send(
            client.build_request("POST", "https://rpc.example", content=REQUEST),
            stream=True,
        )
        if mode == "sync":
            iterator = response.iter_bytes(chunk_size=1024)
            assert next(iterator) == PAYLOADS["rpc_error"]
            assert response.is_closed
            assert outcomes(xray) == {}
            response.close()
            iterator.close()
        else:
            response = await response
            iterator = response.aiter_bytes(chunk_size=1024)
            assert await anext(iterator) == PAYLOADS["rpc_error"]
            assert response.is_closed
            assert outcomes(xray) == {}
            await response.aclose()
            await iterator.aclose()
        assert outcomes(xray) == {"ok": 1}
        assert stream.closed == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_decoded_size_cap_preserves_full_body(mode):
    body = json.dumps(
        {"id": 1, "error": {"code": -32603, "message": "x" * 1000000}}
    ).encode()
    wire = gzip.compress(body)
    transport, _, _ = mock(mode, wire, chunk_size=len(wire))
    xray = Xray(max_body_bytes=160)
    async with client_for(mode, xray, transport) as client:
        response = await post(client, mode, "https://rpc.example")
    assert response.content == body
    assert outcomes(xray) == {"ok": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_encoded_length_above_cap_does_not_skip_small_decoded_body(mode):
    wire = gzip.compress(PAYLOADS["rpc_error"], mtime=0)
    wire = (
        wire[:3] + bytes([wire[3] | 8]) + wire[4:10] + b"name" * 100 + b"\0" + wire[10:]
    )
    assert len(wire) > 160 > len(PAYLOADS["rpc_error"])
    transport, _, _ = mock(mode, wire)
    xray = Xray(max_body_bytes=160)
    async with client_for(mode, xray, transport) as client:
        assert (await post(client, mode, "https://rpc.example")).content == PAYLOADS[
            "rpc_error"
        ]
    assert outcomes(xray) == {"rpc_error": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status,outcome", [(429, "rate_limited"), (500, "http_error")])
async def test_http_status_precedence(mode, status, outcome):
    transport, _, _ = mock(mode, gzip.compress(PAYLOADS["caller_error"]), status=status)
    xray = Xray()
    async with client_for(mode, xray, transport) as client:
        assert (await post(client, mode, "https://rpc.example")).content == PAYLOADS[
            "caller_error"
        ]
    assert outcomes(xray) == {outcome: 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_corrupt_gzip_preserves_decoding_error(mode):
    errors = []
    xray = Xray()
    for telemetry in (None, xray):
        transport, _, _ = mock(mode, b"not gzip")
        async with client_for(mode, telemetry, transport) as client:
            with pytest.raises(httpx.DecodingError) as caught:
                await post(client, mode, "https://rpc.example")
            errors.append((type(caught.value), caught.value.args))
    assert errors[0] == errors[1]
    assert outcomes(xray) == {"rpc_error": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "error,outcome",
    [(httpx.ReadTimeout("timeout"), "timeout"), (httpx.ReadError("broken"), "network")],
)
async def test_encoded_stream_errors_preserve_identity(mode, error, outcome):
    transport, stream, _ = mock(mode, gzip.compress(PAYLOADS["ok"])[:8], error=error)
    xray = Xray()
    async with client_for(mode, xray, transport) as client:
        with pytest.raises(type(error)) as caught:
            await post(client, mode, "https://rpc.example")
        assert caught.value is error
    assert stream.closed == 1
    assert outcomes(xray) == {outcome: 1}


async def test_async_cancellation_during_compressed_body():
    entered = asyncio.Event()
    closed = []

    class Blocked(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield gzip.compress(PAYLOADS["ok"])[:8]
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.append(True)

    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, stream=Blocked()
        )
    )
    xray = Xray()
    async with client_for("async", xray, transport) as client:
        task = asyncio.create_task(post(client, "async", "https://rpc.example"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed == [True]
    assert outcomes(xray) == {"timeout": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_preconsumed_compressed_response_and_batch(mode):
    request = b'[{"id":1,"method":"eth_blockNumber"},{"id":2,"method":"eth_call"}]'
    body = b'[{"id":2,"error":{"code":3,"message":"revert"}},{"id":1,"result":"0x10"}]'
    original = httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(body)
    )
    assert original.is_stream_consumed
    xray = Xray()
    async with client_for(
        mode, xray, httpx.MockTransport(lambda _: original)
    ) as client:
        response = await post(client, mode, "https://rpc.example", request)
        assert response is original and response.content == body
    assert outcomes(xray) == {"ok": 1, "caller_error": 1}


@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_recording_failure_does_not_replace_decoded_result(mode, monkeypatch):
    transport, _, _ = mock(mode, gzip.compress(PAYLOADS["rpc_error"]))
    xray = Xray()
    monkeypatch.setattr(
        xray, "_inc", lambda *_: (_ for _ in ()).throw(RuntimeError("telemetry"))
    )
    async with client_for(mode, xray, transport) as client:
        assert (await post(client, mode, "https://rpc.example")).content == PAYLOADS[
            "rpc_error"
        ]
