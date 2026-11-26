"""EuroBonus progress: card spending towards each card's milestones,
level points towards Silver and Gold in the qualification period, and
points earned per month. Read by the Points page (web/charts.py draws it).

The qualification period is 12 months from the month the member joined
EuroBonus (setting eurobonus_qualification_month, 1-12; January if unset).
"""

from __future__ import annotations

from datetime import date, timedelta

from . import db

LEVELS = [("Silver", 20000), ("Gold", 45000)]      # level points in a qualification period
SETTING = "eurobonus_qualification_month"

SOURCE_LABELS = {"amex": "American Express", "dnb": "DNB Mastercard", "trumf": "Trumf",
                 "sas": "SAS Mastercard", "flights": "Flights", "other": "Other"}
SOURCE_ORDER = list(SOURCE_LABELS)


def qualification_period(month: int, year: int) -> tuple[date, date]:
    """The period that starts in the given year: from the first of `month`
    to the day before the same month a year later."""
    start = date(year, month, 1)
    return start, date(year + 1, month, 1) - timedelta(days=1)


def current_period(month: int, today: date) -> tuple[date, date]:
    year = today.year if today.month >= month else today.year - 1
    return qualification_period(month, year)


def source_of(partner: str, cards: list[tuple[str, str]]) -> str:
    """Which source a EuroBonus posting came from. cards: (code, the card's
    partner name in the history). Level points arrive as "Level points from
    <partner>", so the partner name, less a trailing country, is looked for
    anywhere in the text."""
    p = partner.lower()
    for code, card_partner in cards:
        core = card_partner.lower().removesuffix(" norway").strip()
        if core and core in p:
            return code if code in SOURCE_LABELS else "other"
    if "norgesgruppen" in p or "trumf" in p:
        return "trumf"
    if " - " in partner:
        return "flights"
    return "other"


def cumulative(daily: list[tuple[date, float]]) -> list[tuple[date, float]]:
    out, total = [], 0.0
    for day, value in daily:
        total += value
        out.append((day, total))
    return out


def projection(total: float, start: date, end: date, today: date) -> float | None:
    """The period's total at its pace so far; None before a week has passed
    or once the period is over."""
    if today >= end or today < start + timedelta(days=7):
        return None
    elapsed = (today - start).days + 1
    return total * ((end - start).days + 1) / elapsed


def reached(series: list[tuple[date, float]], amount: float) -> date | None:
    return next((d for d, v in series if v >= amount), None)


async def card_progress(conn, card: str, year: int, today: date) -> dict | None:
    """A card's earning spend through a calendar year against its milestones."""
    start, end = date(year, 1, 1), date(year, 12, 31)
    milestones = await conn.fetch(
        """SELECT spend_minor, reward, level_points FROM points_milestone
           WHERE card = $1 AND valid_from <= $2 AND (valid_to IS NULL OR valid_to >= $2)
           ORDER BY spend_minor""", card, end)
    if not milestones:
        return None
    rows = await conn.fetch(
        """SELECT t.booking_date AS day, sum(-p.earning_minor) AS spend
           FROM points_expected p JOIN txn t ON t.id = p.txn_id
           WHERE p.card = $1 AND t.booking_date BETWEEN $2 AND $3
           GROUP BY 1 ORDER BY 1""", card, start, end)
    cap = await conn.fetchval(
        """SELECT yearly_cap_minor FROM earn_rule WHERE card = $1 AND valid_from <= $2
           AND (valid_to IS NULL OR valid_to >= $2) ORDER BY valid_from DESC LIMIT 1""", card, end)
    series = cumulative([(r["day"], float(r["spend"]) / 100) for r in rows])
    total = series[-1][1] if series else 0.0
    upto = min(today, end)
    projected = projection(total, start, end, today)
    if projected and cap:
        projected = min(projected, float(cap) / 100)          # nothing earns past the cap
    return {
        "start": start, "end": end, "today": upto, "series": series, "total": total,
        "projected": projected,
        "cap": float(cap) / 100 if cap else None,
        "milestones": [{"value": float(m["spend_minor"]) / 100, "reward": m["reward"],
                        "level_points": m["level_points"],
                        "reached": reached(series, float(m["spend_minor"]) / 100)}
                       for m in milestones],
    }


async def level_progress(conn, today: date, year: int | None = None) -> dict:
    """Level points in a qualification period, against Silver and Gold."""
    month = int(await db.get_setting(conn, SETTING) or 1)
    start, end = (qualification_period(month, year) if year else current_period(month, today))
    cards = [(r["code"], r["partner"]) for r in await conn.fetch("SELECT code, partner FROM points_card")]
    rows = await conn.fetch(
        """SELECT day, partner, level_points FROM eb_txn
           WHERE day BETWEEN $1 AND $2 AND level_points <> 0 ORDER BY day""", start, end)
    daily: dict[date, float] = {}
    by_source: dict[str, int] = {}
    for r in rows:
        daily[r["day"]] = daily.get(r["day"], 0) + r["level_points"]
        s = source_of(r["partner"], cards)
        by_source[s] = by_source.get(s, 0) + r["level_points"]
    series = cumulative(sorted(daily.items()))
    total = series[-1][1] if series else 0.0
    return {
        "start": start, "end": end, "today": min(today, end), "series": series, "total": total,
        "projected": projection(total, start, end, today), "month_set": await db.get_setting(conn, SETTING),
        "milestones": [{"value": v, "reward": name, "reached": reached(series, v)} for name, v in LEVELS],
        "sources": [(SOURCE_LABELS[s], by_source[s]) for s in SOURCE_ORDER if by_source.get(s)],
    }


async def earned_monthly(conn, months: int, today: date) -> dict:
    """Bonus points earned per month and source (refunds of award bookings
    and points spent are left out)."""
    first = date(today.year, today.month, 1)
    for _ in range(months - 1):
        first = date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)
    cards = [(r["code"], r["partner"]) for r in await conn.fetch("SELECT code, partner FROM points_card")]
    rows = await conn.fetch(
        """SELECT date_trunc('month', day)::date AS month, partner, sum(bonus_points) AS points
           FROM eb_txn WHERE day >= $1 AND flow = 'earned' AND bonus_points > 0
           GROUP BY 1, 2""", first)
    labels = []
    m = first
    while m <= today:
        labels.append(m)
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    stacks = {s: [0] * len(labels) for s in SOURCE_ORDER}
    index = {m: i for i, m in enumerate(labels)}
    for r in rows:
        if r["month"] in index:
            stacks[source_of(r["partner"], cards)][index[r["month"]]] += int(r["points"])
    return {"months": labels, "stacks": {s: v for s, v in stacks.items() if any(v)},
            "labels": SOURCE_LABELS}
