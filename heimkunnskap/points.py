"""EuroBonus points: what each card purchase should earn, what was received,
and which card would have earned the most.

For every purchase or refund on a card in points_card:

  points      the amount times the card's earn rule on that day (points per
              100 kr in NOK, another rate in foreign currency), unless the
              payee matches an earn_exclusion (a Vipps payment to a person
              is a money transfer) or the card's yearly cap on point-earning
              spend is used up. A refund takes its points back.
  period      the posting month that should pay for it: the purchase date
              moved by the rule's period_shift_days (DNB posts around the
              23rd for purchases from the 25th of the month before).
  best        the most any of the household's cards would have earned for
              it, on that day's rules; American Express only at a merchant
              where it has been used, since not every shop takes it.

report.points_monthly sets expected against received per card and month;
received is the sum of the card's postings in the EuroBonus history.
Points are points: no krone value is put on them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger("heimkunnskap.points")

PREFIX = re.compile(r"^(vipps\*|aer\*|sumup \*|sq \*|ms\* ?|dinter\*|zettle_\*|izettle\*|paypal \*|"
                    r"klarna\*|google \*|apple\.com/bill|sp \*)", re.I)


def merchant_key(counterparty: str | None, description: str | None) -> str:
    """A rough merchant name, to tell whether two purchases were at the same
    merchant: the payee without payment-service prefixes and numbers, its
    first word if that is long enough, else its first two."""
    text = (counterparty or description or "").split(",")[0].lower().strip()
    text = PREFIX.sub("", text)
    words = [w for w in re.split(r"[^a-zæøå]+", text) if w]
    if not words:
        return text[:20]
    return words[0] if len(words[0]) >= 4 else " ".join(words[:2])


@dataclass
class Rule:
    card: str
    valid_from: date
    valid_to: date | None
    rate_nok: float
    rate_foreign: float
    cap: int | None
    shift: int

    def applies(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)

    def rate(self, foreign: bool) -> float:
        return self.rate_foreign if foreign else self.rate_nok


def rule_on(rules: list[Rule], card: str, day: date) -> Rule | None:
    return next((r for r in reversed(rules) if r.card == card and r.applies(day)), None)


def compute(txns: list[dict], card_of: dict[int, str], rules: list[Rule],
            exclusions: list[tuple[re.Pattern, str]], may_be_missing: set[str]) -> list[tuple]:
    """Rows for points_expected. txns: id, account_id, booking_date,
    amount_minor, original_currency, counterparty, description; in date
    order."""
    used_at: dict[str, set[str]] = {}
    for t in txns:
        card = card_of[t["account_id"]]
        if card in may_be_missing:
            used_at.setdefault(card, set()).add(merchant_key(t["counterparty"], t["description"]))
    spent: dict[tuple[str, int], int] = {}
    cards = sorted({r.card for r in rules})
    out = []
    for t in txns:
        card, day, amount = card_of[t["account_id"]], t["booking_date"], t["amount_minor"]
        rule = rule_on(rules, card, day)
        if rule is None:
            continue
        foreign = (t["original_currency"] or "NOK").strip() not in ("", "NOK")
        # the bank text alone: for Vipps shops the payee repeats the name, and
        # "vipps*tabs vipps*tabs" would look like a first and a last name
        text = (t["description"] or t["counterparty"] or "").lower().strip()
        merchant = merchant_key(t["counterparty"], t["description"])
        excluded = next((why for rx, why in exclusions if rx.search(text)), None)
        earning = 0 if excluded else amount
        if not excluded and amount < 0 and rule.cap:
            key = (card, day.year)
            room = max(0, rule.cap - spent.get(key, 0))
            spent[key] = spent.get(key, 0) - amount
            if room < -amount:
                earning = -room
                excluded = "over the yearly cap" if room == 0 else None
        points = -earning * rule.rate(foreign) / 10000
        best_card, best = card, points
        if not excluded and amount < 0:
            for other in cards:
                r = rule_on(rules, other, day)
                if r is None or other == card:
                    continue
                if other in may_be_missing and merchant not in used_at.get(other, set()):
                    continue                        # not known to take this card
                p = -amount * r.rate(foreign) / 10000
                if p > best + 0.005:
                    best_card, best = other, p
        period = (day + timedelta(days=rule.shift)).replace(day=1)
        out.append((t["id"], card, period, earning, foreign, round(points, 2), excluded,
                    best_card, round(best, 2), merchant))
    return out


AWARD = re.compile(r" - |award|redemption|rebooking|upgrade", re.I)
SHOP = re.compile(r"checkout|eurobonus shop|\bshop\b", re.I)
REFUNDER = "Scandinavian Airlines System"
REFUND_WINDOW = 400               # days a refund may come after its booking


def pair_awards(rows: list[dict]) -> dict[int, tuple[str, int | None]]:
    """flow and refund_of per EuroBonus row (id, day, partner, bonus_points,
    level_points). A positive posting from SAS of exactly an earlier award
    booking's points is that booking refunded (changed or cancelled); the
    latest unpaired booking of that amount is the one."""
    out: dict[int, tuple[str, int | None]] = {}
    open_awards: list[dict] = []
    for r in sorted(rows, key=lambda r: (r["day"], r["bonus_points"] > 0, r["id"])):
        pts = r["bonus_points"]
        if pts < 0 and AWARD.search(r["partner"]):
            out[r["id"]] = ("award", None)
            open_awards.append(r)
        elif pts < 0:
            out[r["id"]] = ("shop" if SHOP.search(r["partner"]) else "spent", None)
        elif pts > 0 and r["partner"] == REFUNDER:
            booking = next((a for a in reversed(open_awards)
                            if -a["bonus_points"] == pts
                            and 0 <= (r["day"] - a["day"]).days <= REFUND_WINDOW), None)
            if booking:
                open_awards.remove(booking)
                out[booking["id"]] = ("award_changed", None)
                out[r["id"]] = ("award_refund", booking["id"])
            else:
                out[r["id"]] = ("earned", None)
        elif pts > 0:
            out[r["id"]] = ("earned", None)
        else:
            out[r["id"]] = ("level", None)
    return out


async def classify_eurobonus(conn) -> None:
    rows = [dict(r) for r in await conn.fetch(
        "SELECT id, day, partner, bonus_points, level_points FROM eb_txn")]
    flows = pair_awards(rows)
    await conn.executemany("UPDATE eb_txn SET flow = $2, refund_of = $3 WHERE id = $1",
                           [(i, f, ref) for i, (f, ref) in flows.items()])


async def load_rules(conn) -> list[Rule]:
    return [Rule(r["card"], r["valid_from"], r["valid_to"], float(r["rate_nok"]),
                 float(r["rate_foreign"]), r["yearly_cap_minor"], r["period_shift_days"])
            for r in await conn.fetch("SELECT * FROM earn_rule ORDER BY valid_from, id")]


async def run(conn) -> int:
    """Recompute points_expected for every card purchase, and the award
    pairing in the EuroBonus history."""
    await classify_eurobonus(conn)
    cards = await conn.fetch("SELECT * FROM points_card")
    if not cards:
        return 0
    accounts = await conn.fetch("SELECT id, account_key, coalesce(name, '') AS name FROM account")
    card_of = {}
    for a in accounts:
        for c in cards:
            if re.search(c["account_pattern"], f"{a['account_key']} {a['name']}"):
                card_of[a["id"]] = c["code"]
                break
    exclusions = []
    for e in await conn.fetch("SELECT pattern, note FROM earn_exclusion ORDER BY id"):
        try:
            exclusions.append((re.compile(e["pattern"]), e["note"] or "excluded"))
        except re.error:
            log.warning("earn exclusion %r: bad pattern", e["pattern"])
    rows = []
    if card_of:
        txns = [dict(r) for r in await conn.fetch(
            """SELECT t.id, t.account_id, t.booking_date, t.amount_minor, t.original_currency,
                      t.counterparty, t.description
               FROM txn t LEFT JOIN txn_class c ON c.txn_id = t.id
               WHERE t.account_id = ANY(SELECT id FROM account WHERE cash_account_type = 'CARD')
                 AND c.kind IS NULL ORDER BY t.booking_date, t.id""")
                if r["account_id"] in card_of]
        rows = compute(txns, card_of, await load_rules(conn), exclusions,
                       {c["code"] for c in cards if c["may_be_missing"]})
    async with conn.transaction():
        await conn.execute("DELETE FROM points_expected")
        await conn.executemany(
            """INSERT INTO points_expected (txn_id, card, period, earning_minor, foreign_cur, points,
                                            excluded, best_card, best_points, merchant)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""", rows)
    return len(rows)


def _n(value) -> str:
    return f"{int(value):,}".replace(",", " ")


async def import_checks(conn, today: date | None = None) -> list[str]:
    """After a EuroBonus import: the newest posting, and expected against
    received for each card's last closed month."""
    today = today or date.today()
    newest = await conn.fetchval("SELECT max(day) FROM eb_txn")
    checks = [f"newest posting {newest:%d.%m.%Y}"] if newest else []
    closed = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    for r in await conn.fetch(
            """SELECT c.label, m.expected, m.received FROM report.points_monthly m
               JOIN points_card c ON c.code = m.card WHERE m.period = $1 ORDER BY c.code""", closed):
        checks.append(f"{r['label']}, {closed:%B %Y}: {_n(r['received'])} points received, "
                      f"{_n(r['expected'])} expected")
    return checks
