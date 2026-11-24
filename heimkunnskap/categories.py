"""Spending categories: rules from the category_rule table, applied to the
household's ordinary purchases and payments.

Only spending is categorised: not moves between own accounts, card bills,
saving or loans (txn_class), and not income. Card refunds land in the
category of the purchase text they match; a reimbursed expense ("utlegg")
takes the category of the expense it pays back, so the two cancel out.

A card bill paid from a bank account, whose card has no data for that
period, stands in for the purchases behind it: Card purchases (no detail).
Once the card's file is uploaded the bill pairs with it and drops out.

Rules are Python regular expressions matched against the lower-cased
counterparty and description. The first match by priority wins; no match
means Other. Money in that lowers a cost (electricity support) is matched
by its own text, so a rule puts it in the category of the cost. Recomputed from scratch after every sync, upload and rule
change, so editing a rule re-sorts the whole history.
"""

from __future__ import annotations

import logging
import re
from collections import Counter

log = logging.getLogger("heimkunnskap.categories")

OTHER = "Other"
# A card bill whose card has no data for that period (no file, or older than
# the bank feed): the purchases behind it are spending, only not itemised
NO_DETAIL = "Card purchases (no detail)"


def check_pattern(pattern: str) -> str | None:
    """None if the pattern is usable, else what is wrong with it."""
    if not pattern or len(pattern) > 300:
        return "a rule must be 1 to 300 characters"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"not a valid pattern: {exc}"
    if compiled.search(""):
        return "the pattern matches everything"
    return None


def match_text(counterparty: str | None, description: str | None) -> str:
    return f"{counterparty or ''} {description or ''}".lower()


def compile_rules(rules: list[dict]) -> list[tuple]:
    """rules: id, category_id, pattern, priority -> [(regex, category_id, rule_id)]
    in the order they are tried. A broken pattern is skipped, not fatal."""
    out = []
    for r in sorted(rules, key=lambda r: (r["priority"], r["id"])):
        try:
            out.append((re.compile(r["pattern"]), r["category_id"], r["id"]))
        except re.error:
            log.warning("category rule %s has a broken pattern, skipped", r["id"])
    return out


def categorise(txns: list[dict], rules: list[tuple], other_id: int,
               reimbursed: dict[int, int] | None = None,
               fixed: dict[int, int] | None = None) -> dict[int, tuple]:
    """txns: id, counterparty, description (spending rows only).
    reimbursed: reimbursement txn id -> the expense it pays back, which
    must be among txns for the category to follow it.
    fixed: txn id -> category id, set without rules.
    Returns txn id -> (category_id, rule_id or None)."""
    out = {}
    for t in txns:
        if t["id"] in (fixed or {}):
            out[t["id"]] = (fixed[t["id"]], None)
            continue
        if t["id"] in (reimbursed or {}):
            continue
        text = match_text(t["counterparty"], t["description"])
        out[t["id"]] = next(((cat, rid) for rx, cat, rid in rules if rx.search(text)),
                            (other_id, None))
    by_id = {t["id"]: t for t in txns}
    for rid, expense in (reimbursed or {}).items():
        if expense in out:                     # follows the expense it pays back
            out[rid] = (out[expense][0], None)
            continue
        t = by_id.get(rid)                     # no expense: its own text decides
        text = match_text(t["counterparty"], t["description"]) if t else ""
        out[rid] = next(((cat, r) for rx, cat, r in rules if rx.search(text)), (other_id, None))
    return out


# Spending: ordinary rows going out, anything on a card that is not a
# bill payment (refunds included), reimbursements, and card bills paid from
# a bank account whose card has no data for that period.
SPENDING = """
SELECT t.id, t.counterparty, t.description, c.kind, c.partner_txn_id
FROM txn t
JOIN account a ON a.id = t.account_id
LEFT JOIN txn_class c ON c.txn_id = t.id
WHERE (c.kind IS NULL AND (t.amount_minor < 0 OR a.cash_account_type = 'CARD'))
   OR c.kind = 'reimbursement'
   OR (c.kind = 'card_payment' AND c.partner_txn_id IS NULL AND t.amount_minor < 0
       AND a.cash_account_type IS DISTINCT FROM 'CARD')
"""


async def run(conn) -> dict:
    """Recompute every transaction's category. Returns counts per category."""
    from . import business              # business rules tag payments first
    await business.apply_rules(conn)
    names = {r["id"]: r["name"] for r in await conn.fetch("SELECT id, name FROM category")}
    other_id = next((i for i, n in names.items() if n == OTHER), None)
    if other_id is None:
        other_id = await conn.fetchval(
            "INSERT INTO category (name, sort_order) VALUES ($1, 1000) RETURNING id", OTHER)
        names[other_id] = OTHER
    rules = compile_rules([dict(r) for r in await conn.fetch(
        "SELECT id, category_id, pattern, priority FROM category_rule")])
    no_detail = next((i for i, n in names.items() if n == NO_DETAIL), None)
    if no_detail is None:
        no_detail = await conn.fetchval(
            "INSERT INTO category (name, sort_order) VALUES ($1, 990) RETURNING id", NO_DETAIL)
        names[no_detail] = NO_DETAIL
    rows = [dict(r) for r in await conn.fetch(SPENDING)]
    fixed = {r["id"]: no_detail for r in rows if r["kind"] == "card_payment"}
    whole = {r["txn_id"] for r in await conn.fetch(
        "SELECT txn_id FROM business_expense WHERE share = 1")}
    if whole:
        business_id = next((i for i, n in names.items() if n == business.BUSINESS), None)
        if business_id is None:
            business_id = await conn.fetchval(
                "INSERT INTO category (name, sort_order) VALUES ($1, 980) RETURNING id",
                business.BUSINESS)
            names[business_id] = business.BUSINESS
        fixed.update({r["id"]: business_id for r in rows if r["id"] in whole})
    reimbursed = {r["id"]: r["partner_txn_id"] for r in rows
                  if r["kind"] == "reimbursement" and r["partner_txn_id"]}
    for r in rows:
        if r["kind"] == "reimbursement" and not r["partner_txn_id"]:
            reimbursed[r["id"]] = None
    result = categorise(rows, rules, other_id, reimbursed, fixed)
    async with conn.transaction():
        await conn.execute("DELETE FROM txn_category")
        await conn.executemany(
            "INSERT INTO txn_category (txn_id, category_id, rule_id) VALUES ($1, $2, $3)",
            [(tid, cat, rid) for tid, (cat, rid) in result.items()])
    counts = Counter(names[cat] for cat, _ in result.values())
    log.info("categories: %d rows, %d in Other", len(result), counts.get(OTHER, 0))
    from . import claims                # expense claims split deposits by these categories
    await claims.run(conn)
    from . import points                # points per card purchase
    await points.run(conn)
    return dict(counts)
