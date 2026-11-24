"""Enable Banking API client (https://enablebanking.com/docs/api/reference/).

Every request carries a short-lived RS256 JWT signed with the application's
private key. Read-only: only account information endpoints are used; no
payment initiation exists in this client.

Blocking (urllib); async callers run it with asyncio.to_thread.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import jwt

log = logging.getLogger("heimkunnskap.enablebanking")

DEFAULT_API = "https://api.enablebanking.com"


class ApiError(Exception):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"{path}: HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.path = path

    @property
    def consent_gone(self) -> bool:
        """The session is expired, revoked or unknown: BankID needed again."""
        text = self.message.lower()
        return self.status in (401, 403) and any(
            w in text for w in ("session", "expired", "revoked", "consent", "access"))

    @property
    def rate_limited(self) -> bool:
        return self.status == 429


class EnableBanking:
    def __init__(self, app_id: str, private_key: bytes | str, api: str = DEFAULT_API,
                 opener=None, clock=time.time, timeout: float = 60) -> None:
        if not app_id:
            raise ValueError("EB_APP_ID is not set")
        self.app_id = app_id
        self.private_key = private_key
        self.api = api.rstrip("/")
        self.opener = opener or urllib.request.urlopen
        self.clock = clock
        self.timeout = timeout

    @classmethod
    def from_files(cls, app_id: str, key_file: str, api: str = DEFAULT_API) -> "EnableBanking":
        with open(key_file, "rb") as f:
            return cls(app_id, f.read(), api)

    def token(self) -> str:
        now = int(self.clock())
        return jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
             "iat": now, "exp": now + 3600},
            self.private_key, algorithm="RS256", headers={"kid": self.app_id})

    def request(self, method: str, path: str, params: dict | None = None,
                body: dict | None = None) -> dict:
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                text = resp.read().decode() or "{}"
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("message") or parsed.get("error") or detail
            except ValueError:
                pass
            raise ApiError(exc.code, str(detail)[:500], path) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"unreachable: {exc.reason}", path) from None
        return json.loads(text)

    # -- endpoints

    def aspsps(self, country: str) -> list[dict]:
        return self.request("GET", "/aspsps", {"country": country}).get("aspsps", [])

    def start_auth(self, aspsp_name: str, country: str, redirect_url: str, state: str,
                   valid_until: datetime, psu_type: str = "personal") -> str:
        data = self.request("POST", "/auth", body={
            "access": {"valid_until": valid_until.isoformat(timespec="seconds")},
            "aspsp": {"name": aspsp_name, "country": country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        })
        url = data.get("url")
        if not url:
            raise ApiError(0, "no url in the /auth response", "/auth")
        return url

    def create_session(self, code: str) -> dict:
        return self.request("POST", "/sessions", body={"code": code})

    def session(self, session_id: str) -> dict:
        return self.request("GET", f"/sessions/{session_id}")

    def account_details(self, uid: str) -> dict:
        return self.request("GET", f"/accounts/{uid}/details")

    def balances(self, uid: str) -> dict:
        return self.request("GET", f"/accounts/{uid}/balances")

    def transactions(self, uid: str, date_from: str | None = None,
                     continuation_key: str | None = None, strategy: str | None = None) -> dict:
        return self.request("GET", f"/accounts/{uid}/transactions", {
            "date_from": date_from, "continuation_key": continuation_key,
            "strategy": strategy})


def session_account_uids(session: dict) -> list[str]:
    """Account uids in a session response, which may list uids or objects."""
    uids = []
    for item in session.get("accounts") or []:
        if isinstance(item, str):
            uids.append(item)
        elif isinstance(item, dict) and item.get("uid"):
            uids.append(item["uid"])
    return uids
