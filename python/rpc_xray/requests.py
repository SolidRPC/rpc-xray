import requests
from requests.adapters import BaseAdapter
from urllib3.exceptions import TimeoutError as UrllibTimeout

from .httpx import _Capture


def _failure(error):
    return (
        "timeout" if isinstance(error, (requests.Timeout, UrllibTimeout)) else "network"
    )


class _Raw:
    def __init__(self, inner, capture):
        self.inner, self.capture = inner, capture

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def stream(self, *args, **kwargs):
        try:
            for chunk in self.inner.stream(*args, **kwargs):
                self.capture.feed(chunk)
                yield chunk
        except Exception as error:
            self.capture.finish(failure=_failure(error))
            raise
        else:
            self.capture.finish(complete=True)

    def read(self, amt=None, *args, **kwargs):
        try:
            chunk = self.inner.read(amt, *args, **kwargs)
        except Exception as error:
            self.capture.finish(failure=_failure(error))
            raise
        self.capture.feed(chunk)
        if not chunk or amt is None or amt < 0:
            self.capture.finish(complete=True)
        return chunk

    def close(self):
        try:
            self.inner.close()
        finally:
            self.capture.finish()


class RequestsAdapter(BaseAdapter):
    def __init__(self, xray, *, chain="unknown", adapter=None):
        self.xray, self.chain = xray, chain
        self.inner = adapter if adapter is not None else requests.adapters.HTTPAdapter()

    def send(self, request, **kwargs):
        observation = None
        try:
            if request.method == "POST" and isinstance(
                request.body, (str, bytes, bytearray)
            ):
                observation = self.xray.start(request.url, self.chain, request.body)
        except Exception:
            pass
        try:
            response = self.inner.send(request, **kwargs)
        except Exception as error:
            if observation:
                observation.finish(failure=_failure(error))
            raise
        if observation:
            try:
                length = response.headers.get("Content-Length")
                encoded = (
                    response.headers.get("Content-Encoding", "identity").lower()
                    != "identity"
                )
                if (
                    response.status_code >= 400
                    or (length is not None and int(length) > self.xray.max_body_bytes)
                    or encoded
                ):
                    observation.finish(response.status_code)
                else:
                    response.raw = _Raw(
                        response.raw,
                        _Capture(
                            observation, response.status_code, self.xray.max_body_bytes
                        ),
                    )
            except Exception:
                observation.finish(response.status_code)
        return response

    def close(self):
        self.inner.close()


def instrument_requests(session, xray, *, chain="unknown"):
    for prefix, adapter in list(session.adapters.items()):
        if not isinstance(adapter, RequestsAdapter):
            session.mount(prefix, RequestsAdapter(xray, chain=chain, adapter=adapter))
    return session
