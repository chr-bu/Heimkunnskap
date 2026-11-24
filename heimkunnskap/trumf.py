"""Trumf purchases and receipts from trumf.no, with the household's stored
login session.

trumf.no is a Next.js site signed in with Auth.js: the login lives in the
cookie __Secure-authjs.session-token (split into .0, .1, ... when large)
and lasts a year, renewed as it is used. The worker reads, once a day:

  /api/auth/session            the session: empty when it has ended
  /trumf-profil/kvitteringer   the last 12 months of purchases, in the
                               page's React Server Components payload
                               ("transactionsData")
  /trumf-profil/kvitteringer/<batchId>?...
                               one receipt ("purchaseDetails", with its
                               "varelinjer": product, EAN, quantity,
                               amount, Trumf bonus and the rule behind it)

These are the requests the browser makes (docs/formats/trumf.md); there is
no official API. Requests go one a second. Cookies Trumf sends back are
saved, since a renewed session replaces the old one. When the session ends
or a page no longer reads, a Pushover alert says so, once.

Purchases are kept for good: Trumf itself keeps only 12 months.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time as clock, timedelta
from decimal import Decimal
from http.cookies import SimpleCookie
from zoneinfo import ZoneInfo

from . import db, notify
from .config import settings

log = logging.getLogger("heimkunnskap.trumf")

BASE = "https://www.trumf.no"
SESSION_COOKIE = "__Secure-authjs.session-token"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0 Safari/537.36")
DELAY = 1.0                     # seconds between requests
MAX_RECEIPTS = 200              # per run; the first run takes the rest the next days
ALERT_KEY = "trumf_alert"
FETCH_AT = clock(7, 10)         # daily, local time
LOCK = 727_001                  # advisory lock: one fetch at a time (worker and manual)


class TrumfError(Exception):
    """Trumf answered, but not as expected (a changed page)."""


class SessionEnded(TrumfError):
    """The stored login session no longer works."""


# -- Session cookies

def session_cookies(cookie_header: str) -> dict[str, str]:
    """The Auth.js session pieces from a Cookie header ("a=b; c=d") or from
    lines of name=value; other cookies are dropped."""
    out = {}
    for part in re.split(r"[;\n]", cookie_header):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip().startswith(SESSION_COOKIE) and value.strip():
            out[name.strip()] = value.strip()
    return out


def apply_set_cookies(jar: dict[str, str], headers: list[str]) -> bool:
    """Apply Set-Cookie headers for the session pieces. Returns whether the
    jar changed. A renewed session may come in fewer pieces; the rest are
    then deleted (Max-Age=0 or an expiry in the past, or an empty value)."""
    changed = False
    for header in headers:
        c = SimpleCookie()
        try:
            c.load(header)
        except Exception:
            continue
        for name, morsel in c.items():
            if not name.startswith(SESSION_COOKIE):
                continue
            gone = (morsel.value == "" or morsel["max-age"] in ("0", "-1")
                    or "1970" in (morsel["expires"] or ""))
            if gone and name in jar:
                del jar[name]
                changed = True
            elif not gone and jar.get(name) != morsel.value:
                jar[name] = morsel.value
                changed = True
    return changed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                 # a redirect means: to the login page


class Client:
    """Requests to trumf.no with the session cookies; keeps them current."""

    def __init__(self, jar: dict[str, str], opener=None):
        self.jar = dict(jar)
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.changed = False

    def get(self, path: str, rsc: bool = False) -> str:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "nb,en;q=0.8",
                   "Cookie": "; ".join(f"{k}={v}" for k, v in sorted(self.jar.items()))}
        if rsc:
            headers["RSC"] = "1"
        req = urllib.request.Request(BASE + path, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                self.changed |= apply_set_cookies(self.jar, resp.headers.get_all("Set-Cookie") or [])
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            self.changed |= apply_set_cookies(self.jar, exc.headers.get_all("Set-Cookie") or [])
            if exc.code in (301, 302, 303, 307, 308, 401, 403):
                raise SessionEnded(f"trumf.no answered {exc.code} for {path.split('?')[0]}") from None
            raise TrumfError(f"trumf.no answered {exc.code} for {path.split('?')[0]}") from None


# -- Reading the pages

def _value(v):
    """RSC encodes dates as "$D<iso>" and undefined as "$undefined"."""
    if isinstance(v, str):
        if v == "$undefined":
            return None
        if v.startswith("$D"):
            return v[2:]
    if isinstance(v, dict):
        return {k: _value(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_value(x) for x in v]
    return v


def _flight(text: str) -> str:
    """The RSC payload, also when it comes inside a full HTML page as the
    strings of self.__next_f.push([1, "..."])."""
    if "self.__next_f.push" not in text:
        return text
    parts = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', text)
    return "".join(json.loads(p) for p in parts)


def _after(text: str, key: str):
    flight = _flight(text)
    i = flight.find(f'"{key}":')
    if i < 0:
        raise TrumfError(f"no {key} in the page: trumf.no may have changed")
    value, _ = json.JSONDecoder().raw_decode(flight, i + len(key) + 3)
    return _value(value)


def parse_list(text: str) -> list[dict]:
    data = _after(text, "transactionsData")
    out = []

    def walk(o):
        if isinstance(o, dict):
            if "transaksjonsTidspunkt" in o and "transaksjonKategori" in o:
                out.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    return out


def parse_receipt(text: str) -> dict:
    details = _after(text, "purchaseDetails")
    if not isinstance(details, dict) or "batchId" not in details:
        raise TrumfError("a receipt without its details: trumf.no may have changed")
    return details


def minor(v) -> int:
    return round(float(v or 0) * 100)


def when(v: str) -> datetime:
    """Trumf's times: ISO 8601 in UTC, "2026-11-21T14:29:22.000Z"."""
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def number(v) -> Decimal | None:
    return None if v is None or v == "" else Decimal(str(v))


def receipt_path(t: dict) -> str:
    q = urllib.parse.urlencode({"transactionType": t.get("transaksjonKategori") or "ACCRUAL",
                                "timestamp": t.get("transaksjonsTidspunkt") or "",
                                "description": t.get("beskrivelse") or ""})
    return f"/trumf-profil/kvitteringer/{urllib.parse.quote(t['batchId'])}?{q}"


# -- Storing

def keys(items: list[dict]) -> list[str]:
    """Each purchase's key: Trumf's batchId. Transfers to EuroBonus
    (CONSUME) come without one, so they get kind, time and amount, with a
    count for any that are alike."""
    seen: dict[str, int] = {}
    out = []
    for t in items:
        if t.get("batchId"):
            out.append(t["batchId"])
            continue
        base = (f"{(t.get('transaksjonKategori') or 'x').lower()}:{t.get('transaksjonsTidspunkt')}"
                f":{minor(t.get('belop'))}:{minor(t.get('bonus'))}")
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}:{seen[base]}")
    return out


async def store_purchases(conn, items: list[dict]) -> int:
    before = await conn.fetchval("SELECT count(*) FROM trumf_txn")
    await conn.executemany(
        """INSERT INTO trumf_txn (batch_id, member_id, at, category, partner, store, amount_minor,
                                  bonus_minor, savings_minor, has_receipt, raw)
           VALUES ($1, $2, $3::timestamptz, $4, $5, $6, $7, $8, $9, $10, $11::jsonb)
           ON CONFLICT (batch_id) DO UPDATE SET bonus_minor = excluded.bonus_minor,
               savings_minor = excluded.savings_minor, has_receipt = excluded.has_receipt,
               raw = excluded.raw""",
        [(key, str(t.get("medlemId") or ""), when(t["transaksjonsTidspunkt"]),
          t.get("transaksjonKategori") or "", t.get("partnerId") or "", t.get("beskrivelse") or "",
          minor(t.get("belop")), minor(t.get("bonus")), minor(t.get("totaleBesparelser")),
          bool(t.get("harKvittering")) and bool(t.get("batchId")), json.dumps(t, ensure_ascii=False))
         for key, t in zip(keys(items), items)])
    return await conn.fetchval("SELECT count(*) FROM trumf_txn") - before


async def store_receipt(conn, batch_id: str, details: dict) -> int:
    lines = details.get("varelinjer") or []
    rows = []
    for n, v in enumerate(lines, 1):
        ean = v.get("ean")
        rows.append((batch_id, n, v.get("produktBeskrivelse") or v.get("vareTekst") or "",
                     None if ean in (None, "") else str(ean), number(v.get("antall")),
                     v.get("enhetsType"), minor(v.get("belop")),
                     minor(v.get("bonus")), number(v.get("bonusProsent")), number(v.get("momsProsent")),
                     bool(v.get("ukjentVare")),
                     json.dumps(v.get("bonusregler") or [], ensure_ascii=False),
                     json.dumps(v.get("besparelser") or [], ensure_ascii=False)))
    async with conn.transaction():
        await conn.execute("DELETE FROM trumf_line WHERE batch_id = $1", batch_id)
        await conn.executemany(
            """INSERT INTO trumf_line (batch_id, line_no, product, ean, quantity, unit, amount_minor,
                                       bonus_minor, bonus_pct, vat_pct, unknown_item, rules, savings)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13::jsonb)
               ON CONFLICT (batch_id, line_no) DO NOTHING""", rows)
        await conn.execute("UPDATE trumf_txn SET receipt_fetched_at = now() WHERE batch_id = $1",
                           batch_id)
    return len(rows)


async def match_payments(conn) -> int:
    """Each purchase to the card payment it was: the same amount, booked on
    the day or up to four days later, the store's chain in the bank text if
    possible. Each payment once."""
    purchases = await conn.fetch(
        """SELECT batch_id, (at AT TIME ZONE 'Europe/Oslo')::date AS day, amount_minor, partner, store
           FROM trumf_txn WHERE category = 'ACCRUAL' AND amount_minor > 0 ORDER BY at""")
    if not purchases:
        return 0
    taken: set[int] = set()
    rows = []
    for p in purchases:
        cands = await conn.fetch(
            """SELECT t.id, t.booking_date, coalesce(t.counterparty, '') || ' ' || t.description AS text
               FROM txn t LEFT JOIN txn_class c ON c.txn_id = t.id
               WHERE c.kind IS NULL AND t.amount_minor = $1
                 AND t.booking_date BETWEEN $2 AND $2 + 4 ORDER BY t.booking_date, t.id""",
            -p["amount_minor"], p["day"])
        chain = (p["store"].split() or [""])[0].lower()
        free = [c for c in cands if c["id"] not in taken]
        best = next((c for c in free if chain and chain in c["text"].lower()), free[0] if free else None)
        if best:
            taken.add(best["id"])
        rows.append((p["batch_id"], best["id"] if best else None))
    await conn.executemany("UPDATE trumf_txn SET txn_id = $2 WHERE batch_id = $1", rows)
    return sum(1 for _, t in rows if t)


# -- The session in the database

async def load_jar(conn) -> dict[str, str] | None:
    raw = await conn.fetchval("SELECT cookies FROM trumf_session WHERE id = 1")
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


async def save_jar(conn, jar: dict[str, str], ok: bool | None = None, error: str | None = None) -> None:
    await conn.execute(
        """INSERT INTO trumf_session (id, cookies, updated_at, last_ok_at, last_error)
           VALUES (1, $1::jsonb, now(), CASE WHEN $2 THEN now() END, $3)
           ON CONFLICT (id) DO UPDATE SET cookies = excluded.cookies, updated_at = now(),
               last_ok_at = CASE WHEN $2 THEN now() ELSE trumf_session.last_ok_at END,
               last_error = $3""",
        json.dumps(jar), bool(ok), error)


async def set_session(conn, cookie_header: str) -> int:
    jar = session_cookies(cookie_header)
    if not jar:
        raise ValueError(f"no {SESSION_COOKIE} cookie in what was pasted")
    await save_jar(conn, jar)
    await db.set_setting(conn, ALERT_KEY, "")
    return len(jar)


async def _alert_once(conn, state: str, title: str, body: str) -> None:
    if await db.get_setting(conn, ALERT_KEY) == state:
        return
    notify.send(title, body)
    await db.set_setting(conn, ALERT_KEY, state)


# -- The run

async def run(conn, client: Client | None = None, max_receipts: int = MAX_RECEIPTS,
              delay: float = DELAY) -> dict:
    """Fetch new purchases and receipts. Returns counts, or why not. One
    fetch at a time: the worker's daily one and manual trumf --fetch."""
    if not await conn.fetchval("SELECT pg_try_advisory_lock($1)", LOCK):
        return {"status": "already running (the worker's daily fetch, or another manual one)"}
    try:
        return await _run(conn, client, max_receipts, delay)
    finally:
        await conn.fetchval("SELECT pg_advisory_unlock($1)", LOCK)


async def _run(conn, client: Client | None, max_receipts: int, delay: float) -> dict:
    jar = await load_jar(conn)
    if jar is None and client is None:
        return {"status": "no session"}
    client = client or Client(jar)
    loop = asyncio.get_running_loop()

    async def get(path: str, rsc: bool = False) -> str:
        text = await loop.run_in_executor(None, lambda: client.get(path, rsc))
        if delay:
            await asyncio.sleep(delay)
        return text

    async def page(path: str, key: str) -> str:
        """The page as a browser loads it; its RSC payload if the data is
        not in the HTML."""
        text = await get(path)
        if f'{key}\\":' in text or f'"{key}":' in text:
            return text
        return await get(path, rsc=True)

    counts = {"status": "ok", "purchases": 0, "new": 0, "receipts": 0, "lines": 0, "left": 0}
    try:
        session = json.loads(await get("/api/auth/session") or "null") or {}
        # trumf.no's session has an empty "user"; the tokens show the login
        if session.get("error") or not (session.get("accessToken") or session.get("memberId")):
            raise SessionEnded("the login session has ended (trumf.no's session had "
                               + (", ".join(sorted(session)) or "nothing") + ")")
        items = parse_list(await page("/trumf-profil/kvitteringer", "transactionsData"))
        counts["purchases"] = len(items)
        counts["new"] = await store_purchases(conn, items)
        batch = await db.start_batch(conn, source="trumf", kind="fetch")
        await db.store_raw(conn, batch, "trumf_list",
                           content=json.dumps(items, ensure_ascii=False).encode(), filename="kvitteringer.json")
        todo = await conn.fetch(
            """SELECT batch_id, raw FROM trumf_txn WHERE has_receipt AND receipt_fetched_at IS NULL
               ORDER BY at DESC""")
        for row in todo[:max_receipts]:
            t = json.loads(row["raw"]) if isinstance(row["raw"], str) else dict(row["raw"])
            details = parse_receipt(await page(receipt_path(t), "purchaseDetails"))
            await db.store_raw(conn, batch, "trumf_receipt",
                               content=json.dumps(details, ensure_ascii=False).encode(),
                               filename=f"{row['batch_id']}.json")
            counts["lines"] += await store_receipt(conn, row["batch_id"], details)
            counts["receipts"] += 1
            if client.changed:
                await save_jar(conn, client.jar)
        counts["left"] = max(0, len(todo) - max_receipts)
        await db.finish_batch(conn, batch, pages=1 + counts["receipts"], seen=len(items), added=counts["new"])
        await match_payments(conn)
        await save_jar(conn, client.jar, ok=True)
        await db.set_setting(conn, ALERT_KEY, "")
    except SessionEnded as exc:
        await save_jar(conn, client.jar, error=str(exc))
        await _alert_once(conn, "session", "Trumf login has ended",
                          "Log in on trumf.no, copy the cookie again and run: manual trumf-session")
        return {**counts, "status": "session ended", "error": str(exc)}
    except (TrumfError, json.JSONDecodeError, urllib.error.URLError, TimeoutError) as exc:
        await save_jar(conn, client.jar, error=f"{type(exc).__name__}: {exc}"[:300])
        await _alert_once(conn, "error", "Trumf fetch failed", f"{type(exc).__name__}: {exc}"[:300])
        return {**counts, "status": "failed", "error": str(exc)}
    log.info("trumf: %s", counts)
    return counts


async def daily(conn) -> dict | None:
    """The worker's daily fetch, if a session is stored."""
    if await conn.fetchval("SELECT 1 FROM trumf_session WHERE id = 1") is None:
        return None
    return await run(conn)


async def loop() -> None:
    """Fetch once a day at FETCH_AT, and at start if a day's fetch was missed."""
    tz = ZoneInfo(settings.timezone)
    while True:
        now = datetime.now(tz)
        due = datetime.combine(now.date(), FETCH_AT, tz)
        try:
            conn = await db.connect()
            try:
                last = await conn.fetchval("SELECT last_ok_at FROM trumf_session WHERE id = 1")
                if now >= due and (last is None or last < due):
                    await daily(conn)
            finally:
                await conn.close()
        except Exception:
            log.exception("Trumf fetch failed")
        target = due if now < due else due + timedelta(days=1)
        await asyncio.sleep(max(60, (target - datetime.now(tz)).total_seconds()))
