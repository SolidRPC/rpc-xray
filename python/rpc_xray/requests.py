import requests
from requests.adapters import BaseAdapter
from urllib3.exceptions import TimeoutError as UrllibTimeout

from .httpx import _Capture


def _failure(error):
    return (
        "timeout" if isinstance(error, (requests.Timeout, UrllibTimeout)) else "network"
    )


class _Raw:
    def __init__(self, inner, capture, encodings=()):
        self.inner, self.capture, self.encodings = inner, capture, encodings

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @property
    def decode_content(self):
        return self.inner.decode_content

    @decode_content.setter
    def decode_content(self, value):
        self.inner.decode_content = value

    def _feed(self, chunk, decode_content):
        try:
            if self.encodings:
                decoding = (
                    self.inner.decode_content
                    if decode_content is None
                    else decode_content
                )
                supported = getattr(self.inner, "CONTENT_DECODERS", ())
                if not decoding or any(e not in supported for e in self.encodings):
                    self.capture.finish()
                    return
            self.capture.feed(chunk)
        except Exception:
            self._finish()

    def _finish(self, **kwargs):
        try:
            self.capture.finish(**kwargs)
        except Exception:
            pass

    def stream(self, *args, **kwargs):
        decode_content = kwargs.get(
            "decode_content", args[1] if len(args) > 1 else None
        )
        try:
            for chunk in self.inner.stream(*args, **kwargs):
                self._feed(chunk, decode_content)
                yield chunk
        except Exception as error:
            self._finish(failure=_failure(error))
            raise
        else:
            self._feed(b"", decode_content)
            self._finish(complete=True)

    def read(self, amt=None, *args, **kwargs):
        decode_content = kwargs.get("decode_content", args[0] if args else None)
        try:
            chunk = self.inner.read(amt, *args, **kwargs)
        except Exception as error:
            self._finish(failure=_failure(error))
            raise
        if amt != 0:
            self._feed(chunk, decode_content)
            if not chunk or amt is None or amt < 0:
                self._finish(complete=True)
        return chunk

    def close(self):
        try:
            self.inner.close()
        finally:
            self._finish()


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
                encodings = tuple(
                    encoding.strip().lower()
                    for encoding in response.headers.get("Content-Encoding", "").split(
                        ","
                    )
                    if encoding.strip().lower() not in ("", "identity")
                )
                if response.status_code >= 400 or (
                    not encodings
                    and length is not None
                    and int(length) > self.xray.max_body_bytes
                ):
                    observation.finish(response.status_code)
                else:
                    response.raw = _Raw(
                        response.raw,
                        _Capture(
                            observation, response.status_code, self.xray.max_body_bytes
                        ),
                        encodings,
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
