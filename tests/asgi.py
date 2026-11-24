"""A minimal ASGI test client (Starlette's TestClient needs httpx, which
the image does not ship). Keeps cookies between requests."""

from __future__ import annotations

import json
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


class Response:
    def __init__(self, status: int, headers: list, body: bytes) -> None:
        self.status = status
        self.header_list = headers
        self.headers = {k: v for k, v in headers}
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode()

    def json(self):
        return json.loads(self.body)


class Client:
    def __init__(self, app, client_ip: str = "127.0.0.1") -> None:
        self.app = app
        self.client_ip = client_ip
        self.cookies: dict[str, str] = {}

    async def request(self, method: str, url: str, headers: dict | None = None,
                      body: bytes | str | dict | list | None = None) -> Response:
        parts = urlsplit(url)
        hdrs = {"host": "heim.test"}
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            hdrs["content-type"] = "application/json"
        if isinstance(body, str):
            body = body.encode()
        body = body or b""
        if body:
            hdrs["content-length"] = str(len(body))
        if self.cookies:
            hdrs["cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "scheme": "http", "path": parts.path,
            "raw_path": parts.path.encode(), "query_string": parts.query.encode(),
            "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
            "client": (self.client_ip, 50000), "server": ("heim.test", 80),
        }
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        status, out_headers, chunks = 0, [], []

        async def send(message):
            nonlocal status, out_headers
            if message["type"] == "http.response.start":
                status = message["status"]
                out_headers = [(k.decode().lower(), v.decode()) for k, v in message["headers"]]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        for k, v in out_headers:
            if k == "set-cookie":
                c = SimpleCookie()
                c.load(v)
                for name, morsel in c.items():
                    if morsel.value and "max-age=0" not in v.lower():
                        self.cookies[name] = morsel.value
                    else:
                        self.cookies.pop(name, None)
        return Response(status, out_headers, b"".join(chunks))

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw):
        return await self.request("POST", url, **kw)


def multipart(fields: dict, files: list[tuple[str, str, bytes]]) -> tuple[bytes, str]:
    """A multipart/form-data body: fields, then (field, filename, content) files."""
    boundary = "----heimkunnskap-test-boundary"
    parts = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                     f"{value}\r\n".encode())
    for field, filename, content in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                     f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
                     .encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
