import asyncio

import httpx

from .core import Xray


class _Capture:
    def __init__(self, observation, status, cap):
        self.observation, self.status, self.cap = observation, status, cap
        self.body = bytearray()

    def feed(self, chunk):
        try:
            if self.observation.done:
                return
            if len(self.body) + len(chunk) > self.cap:
                self.finish()
                self.body.clear()
            else:
                self.body.extend(chunk)
        except Exception:
            self.finish()

    def finish(self, complete=False, failure=None, invalid=False):
        try:
            body = b"" if invalid else bytes(self.body) if complete else None
            self.observation.finish(self.status, body, failure)
        except Exception:
            pass


class _HttpxCapture(_Capture):
    def __init__(self, observation, status, cap, encoded):
        super().__init__(observation, status, cap)
        self.encoded = encoded
        self.decoding = 0
        self.raw_complete = False

    def raw_feed(self, chunk):
        if not self.decoding and not self.encoded:
            self.feed(chunk)

    def raw_finish(self):
        self.raw_complete = True
        if not self.decoding:
            self.finish(complete=not self.encoded)

    def raw_close(self):
        if not self.decoding or not self.raw_complete:
            self.finish()

    def error(self, error):
        if isinstance(error, httpx.DecodingError):
            self.finish(invalid=True)
        elif isinstance(error, GeneratorExit):
            self.finish()
        else:
            self.finish(
                failure="timeout"
                if isinstance(error, (httpx.TimeoutException, asyncio.CancelledError))
                else "network"
            )


class _Stream(httpx.SyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    def __iter__(self):
        try:
            for chunk in self.inner:
                self.capture.raw_feed(chunk)
                yield chunk
        except Exception as error:
            self.capture.error(error)
            raise
        else:
            self.capture.raw_finish()

    def close(self):
        try:
            self.inner.close()
        finally:
            self.capture.raw_close()


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.capture.raw_feed(chunk)
                yield chunk
        except BaseException as error:
            self.capture.error(error)
            raise
        else:
            self.capture.raw_finish()

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            self.capture.raw_close()


def _observe_decoded(response, capture, async_mode):
    if async_mode:
        original_iter = response.aiter_bytes
        original_close = response.aclose

        async def aiter_bytes(chunk_size=None):
            capture.decoding += 1
            complete = False
            try:
                async for chunk in original_iter(chunk_size):
                    capture.feed(chunk)
                    yield chunk
                complete = True
            except BaseException as error:
                capture.error(error)
                raise
            finally:
                capture.decoding -= 1
                capture.finish(complete=complete)

        async def aclose():
            was_closed = response.is_closed
            try:
                await original_close()
            finally:
                if was_closed:
                    capture.finish()

        response.aiter_bytes = aiter_bytes
        response.aclose = aclose
    else:
        original_iter = response.iter_bytes
        original_close = response.close

        def iter_bytes(chunk_size=None):
            capture.decoding += 1
            complete = False
            try:
                for chunk in original_iter(chunk_size):
                    capture.feed(chunk)
                    yield chunk
                complete = True
            except BaseException as error:
                capture.error(error)
                raise
            finally:
                capture.decoding -= 1
                capture.finish(complete=complete)

        def close():
            was_closed = response.is_closed
            try:
                original_close()
            finally:
                if was_closed:
                    capture.finish()

        response.iter_bytes = iter_bytes
        response.close = close


def _begin(xray, chain, request):
    try:
        if request.method == "POST":
            return xray.start(str(request.url), chain, request.content)
    except Exception:
        pass
    return None


def _attach(xray, observation, response, async_mode=False):
    if observation is None:
        return response
    try:
        size = response.headers.get("content-length")
        encoded = any(
            encoding.strip().lower() not in ("", "identity")
            for encoding in response.headers.get("content-encoding", "").split(",")
        )
        if response.status_code >= 400 or (
            not encoded and size is not None and int(size) > xray.max_body_bytes
        ):
            observation.finish(response.status_code)
        elif response.is_stream_consumed:
            observation.finish(response.status_code, response.content)
        else:
            capture = _HttpxCapture(
                observation, response.status_code, xray.max_body_bytes, encoded
            )
            _observe_decoded(response, capture, async_mode)
            response.stream = (_AsyncStream if async_mode else _Stream)(
                response.stream, capture
            )
    except Exception:
        observation.finish(response.status_code)
    return response


class Transport(httpx.BaseTransport):
    def __init__(self, xray: Xray, *, chain="unknown", transport=None):
        self.xray, self.chain = xray, chain
        self.inner = transport if transport is not None else httpx.HTTPTransport()

    def handle_request(self, request):
        observation = _begin(self.xray, self.chain, request)
        try:
            response = self.inner.handle_request(request)
        except Exception as error:
            if observation:
                observation.finish(
                    failure="timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "network"
                )
            raise
        return _attach(self.xray, observation, response)

    def close(self):
        self.inner.close()


class AsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, xray: Xray, *, chain="unknown", transport=None):
        self.xray, self.chain = xray, chain
        self.inner = transport if transport is not None else httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        observation = _begin(self.xray, self.chain, request)
        try:
            response = await self.inner.handle_async_request(request)
        except BaseException as error:
            if observation:
                observation.finish(
                    failure="timeout"
                    if isinstance(
                        error, (httpx.TimeoutException, asyncio.CancelledError)
                    )
                    else "network"
                )
            raise
        return _attach(self.xray, observation, response, True)

    async def aclose(self):
        await self.inner.aclose()
