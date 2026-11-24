"""A fake Enable Banking API for tests: an opener for urllib that answers
from a dict, and a key pair generated on the fly."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def key_pair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """routes: {(method, path): handler(query, body) -> dict | (status, dict)}"""

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls = []

    def __call__(self, req, timeout=None):
        parsed = urllib.parse.urlsplit(req.full_url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), parsed.path, query, body,
                           req.get_header("Authorization")))
        handler = self.routes.get((req.get_method(), parsed.path))
        if handler is None:
            result = (404, {"message": "not found"})
        else:
            result = handler(query, body)
        status, payload = result if isinstance(result, tuple) else (200, result)
        data = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(data))
        return _Resp(data)
