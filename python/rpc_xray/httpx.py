import httpx

from .core import Xray


class _Capture:
    def __init__(self, observation, status, cap):
        self.observation, self.status, self.cap = observation, status, cap
        self.body = bytearray()

    def feed(self, chunk):
        if self.observation.done:
            return
        if len(self.body) + len(chunk) > self.cap:
            self.finish()
            self.body.clear()
        else:
            self.body.extend(chunk)

    def finish(self, complete=False, failure=None):
        self.observation.finish(
            self.status, bytes(self.body) if complete else None, failure
        )


class _Stream(httpx.SyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    def __iter__(self):
        try:
            for chunk in self.inner:
                self.capture.feed(chunk)
                yield chunk
        except Exception as error:
            self.capture.finish(
                failure="timeout"
                if isinstance(error, httpx.TimeoutException)
                else "network"
            )
            raise
        else:
            self.capture.finish(complete=True)

    def close(self):
        try:
            self.inner.close()
        finally:
            self.capture.finish()


class _AsyncStream(httpx.AsyncByteStream):
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    async def __aiter__(self):
        try:
            async for chunk in self.inner:
                self.capture.feed(chunk)
                yield chunk
        except BaseException as error:
            import asyncio

            self.capture.finish(
                failure="timeout"
                if isinstance(error, (httpx.TimeoutException, asyncio.CancelledError))
                else "network"
            )
            raise
        else:
            self.capture.finish(complete=True)

    async def aclose(self):
        try:
            await self.inner.aclose()
        finally:
            self.capture.finish()


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
        encoded = response.headers.get("content-encoding", "identity") != "identity"
        if (
            response.status_code >= 400
            or encoded
            or (size is not None and int(size) > xray.max_body_bytes)
        ):
            observation.finish(response.status_code)
        elif response.is_stream_consumed:
            observation.finish(response.status_code, response.content)
        else:
            capture = _Capture(observation, response.status_code, xray.max_body_bytes)
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
            import asyncio

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
