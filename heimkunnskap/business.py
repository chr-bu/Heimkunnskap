"""The sole proprietorship's expenses and VAT.

Expenses are household payments tagged as the business's, by hand or by a
rule on the payee, wholly or a share of them. The owner's tag decides: no
receipt is asked for. A tagged expense

  - lowers the business profit in the tax estimate by its cost less input VAT;
  - carries input VAT at its rate (25% unless said otherwise, or an amount
    given by hand), from the VAT registration date; before it, the VAT is
    part of the cost;
  - moves to the category Business when it is wholly the business's, out of
    the household's spending categories (a share stays where it was).

VAT is reported per two-month term (report.vat_terms): output VAT from the
deposits into the business-income account, less input VAT, less what was
paid (Innkrevingsmynd.-Mva) or refunded (MVA Tilgode). A term that has ended
with VAT outstanding gets a reminder when it ends, three days before its
due date, and the day after if still unpaid. Reminders go out in the
daytime (REMIND_AT), not with the nightly sync.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import categories, db, notify
from .config import settings

log = logging.getLogger("heimkunnskap.business")

BUSINESS = "Business"
RATES = (25, 15, 12, 0)
REMIND_AT = time(9, 0)
TERM_NAMES = {1: "January-February", 3: "March-April", 5: "May-June", 7: "July-August",
              9: "September-October", 11: "November-December"}
SETTLED = 100                   # øre: a term within 1 kr of zero is settled
ALERT_WINDOW = 30               # days after the due date a term is still chased


def term_name(start: date) -> str:
    return f"{TERM_NAMES[start.month]} {start.year}"


async def apply_rules(conn) -> int:
    """Tag the payments each rule matches; tags made by hand win, and a
    payment takes the first rule that matches. Returns how many rule tags."""
    rules = await conn.fetch("SELECT id, pattern, share, vat_rate FROM business_rule ORDER BY id")
    compiled = []
    for r in rules:
        try:
            compiled.append((r, re.compile(r["pattern"])))
        except re.error:
            log.warning("business rule %s: bad pattern", r["id"])
    async with conn.transaction():
        await conn.execute("DELETE FROM business_expense WHERE rule_id IS NOT NULL")
        if not compiled:
            return 0
        txns = await conn.fetch(
            """SELECT t.id, t.counterparty, t.description FROM txn t
               LEFT JOIN txn_class c ON c.txn_id = t.id
               LEFT JOIN business_expense e ON e.txn_id = t.id
               WHERE c.kind IS NULL AND e.txn_id IS NULL""")
        rows = []
        for t in txns:
            text = categories.match_text(t["counterparty"], t["description"])
            hit = next((r for r, rx in compiled if rx.search(text)), None)
            if hit:
                rows.append((t["id"], hit["share"], hit["vat_rate"], hit["id"]))
        await conn.executemany(
            """INSERT INTO business_expense (txn_id, share, vat_rate, rule_id)
               VALUES ($1, $2, $3, $4) ON CONFLICT (txn_id) DO NOTHING""", rows)
    return len(rows)


def check(share: float, vat_rate: float) -> str | None:
    if not 0 < share <= 1:
        return "the share is a number above 0 and at most 1 (0.5 is half)"
    if vat_rate not in RATES:
        return "the VAT rate is 25, 15, 12 or 0"
    return None


async def tag(conn, txn_id: int, share: float = 1.0, vat_rate: float = 25,
              vat_kr: float | None = None, note: str = "") -> str:
    """Tag a payment as the business's. Raises ValueError with the reason."""
    problem = check(share, vat_rate)
    if problem:
        raise ValueError(problem)
    t = await conn.fetchrow(
        """SELECT t.id, t.amount_minor, t.booking_date, t.description, c.kind FROM txn t
           LEFT JOIN txn_class c ON c.txn_id = t.id WHERE t.id = $1""", txn_id)
    if t is None:
        raise ValueError(f"no transaction with id {txn_id}")
    if t["kind"]:
        raise ValueError(f"that transaction is a {t['kind'].replace('_', ' ')}, not a purchase")
    vat = None if vat_kr is None else round(vat_kr * 100)
    if vat is not None and abs(vat) > abs(t["amount_minor"] * share):
        raise ValueError("the VAT cannot be more than the tagged amount")
    await conn.execute(
        """INSERT INTO business_expense (txn_id, share, vat_rate, vat_minor, note)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (txn_id) DO UPDATE SET share = excluded.share, vat_rate = excluded.vat_rate,
               vat_minor = excluded.vat_minor, note = excluded.note, rule_id = NULL""",
        txn_id, share, vat_rate, vat, note)
    await refresh(conn)
    return f"{t['booking_date']} {t['description'][:50]}: business"


async def untag(conn, txn_id: int) -> str:
    rule = await conn.fetchval("SELECT rule_id FROM business_expense WHERE txn_id = $1", txn_id)
    if rule is not None:
        raise ValueError(f"tagged by rule {rule}: delete or narrow that rule instead")
    done = await conn.execute("DELETE FROM business_expense WHERE txn_id = $1", txn_id)
    await refresh(conn)
    return "untagged" if done.endswith("1") else "nothing to untag"


async def add_rule(conn, pattern: str, share: float = 1.0, vat_rate: float = 25,
                   note: str = "") -> int:
    problem = categories.check_pattern(pattern) or check(share, vat_rate)
    if problem:
        raise ValueError(problem)
    rid = await conn.fetchval(
        "INSERT INTO business_rule (pattern, share, vat_rate, note) VALUES ($1, $2, $3, $4) "
        "RETURNING id", pattern, share, vat_rate, note)
    await refresh(conn)
    return rid


async def delete_rule(conn, rule_id: int) -> None:
    await conn.execute("DELETE FROM business_rule WHERE id = $1", rule_id)
    await refresh(conn)


async def refresh(conn) -> None:
    """Rules, categories (Business), the deposit splits and the tax estimate."""
    await categories.run(conn)          # applies the rules first
    from . import tax
    await tax.refresh(conn)


def status(term: dict, today: date) -> str:
    outstanding = round(term["outstanding"] * 100)
    if today <= term["term_end"]:
        return "running"
    if abs(outstanding) < SETTLED:
        return "settled"
    if outstanding < 0:
        return "refund due" if term["net"] < 0 else "overpaid"
    return "due" if today <= term["due"] else "overdue"


async def terms(conn, today: date | None = None) -> list[dict]:
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    rows = [dict(r) for r in await conn.fetch("SELECT * FROM report.vat_terms ORDER BY term_start")]
    for r in rows:
        r["name"] = term_name(r["term_start"])
        r["status"] = status(r, today)
    return rows


def _kr(v) -> str:
    return f"{round(float(v)):,}".replace(",", " ")


async def check_alerts(conn, today: date | None = None) -> list[str]:
    """Reminders for terms with VAT to pay: once when the term has ended,
    once three days before it is due, once the day after if still unpaid.
    Each is sent once (settings vat_alert:<term>:<stage>)."""
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    sent = []
    for t in await terms(conn, today):
        if t["status"] not in ("due", "overdue"):
            continue
        days = (t["due"] - today).days
        if days < -ALERT_WINDOW:
            continue                                # an old term: not chased
        stage = "overdue" if days < 0 else "soon" if days <= 3 else "ended"
        key = f"vat_alert:{t['term_start']}:{stage}"
        if await db.get_setting(conn, key):
            continue
        amount = _kr(t["outstanding"])
        if stage == "overdue":
            title, body = (f"VAT for {t['name']} is overdue",
                           f"About {amount} kr was due {t['due']:%d.%m.%Y} and no payment is seen.")
        else:
            title = f"VAT for {t['name']}: about {amount} kr"
            body = (f"Due {t['due']:%d.%m.%Y}. Output VAT {_kr(t['output_vat'])} kr, input VAT "
                    f"{_kr(t['input_vat'])} kr from tagged expenses.")
        notify.send(title, f"{body} {settings.public_url}/business".strip())
        await db.set_setting(conn, key, today.isoformat())
        sent.append(title)
    return sent


async def loop() -> None:
    """Daytime checks, once a day at REMIND_AT, and at start if later."""
    tz = ZoneInfo(settings.timezone)
    while True:
        now = datetime.now(tz)
        if now.time() >= REMIND_AT:
            try:
                conn = await db.connect()
                try:
                    await check_alerts(conn, now.date())
                finally:
                    await conn.close()
            except Exception:
                log.exception("VAT reminders failed")
        target = datetime.combine(now.date() + timedelta(days=1), REMIND_AT, tz)
        await asyncio.sleep(max(60, (target - datetime.now(tz)).total_seconds()))
