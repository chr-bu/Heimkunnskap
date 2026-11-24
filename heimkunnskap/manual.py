"""Tell Heimkunnskap what the bank text cannot: which payments were loans the
household made, and which were investments, so neither counts as spending.

  find TEXT             list transactions whose text contains TEXT, with ids
  lend ID --repaid-by P payment ID was money lent out; incoming payments
       [--until DATE]   matching the regex P are its repayments, up to DATE
                        if given (run it again to change the pattern or date)
  mark ID KIND          payment ID is saving, loan or reimbursement
  split ID --principal KR --interest KR [--fees KR]
                        a payment to a loan that the bank split differently
                        from the text (interest taken out of a direct payment
                        to the rammelån): the three amounts must add up to it
  check                 months with a direct payment to the rammelån but no
                        interest debit for it: the interest was probably
                        netted out of a payment, so split it (see split)
  unmark ID             undo lend, mark or split for payment ID
  claims                expense claims: each outlay and the card payment it
                        was, and the deposit that paid the claim
  claim-line LINE TXN   outlay LINE (from claims) was card payment TXN;
                        TXN "none": not paid by card; "auto": match again
  claim-payout NO TXN   claim NO was paid by deposit TXN ("none", "auto");
                        several claims may share one deposit
  list                  every loan and mark made here
  vat ID KR [--term YYYY-MM]
                        business deposit ID holds KR of VAT (when not 20%),
                        for the VAT term holding that month if not its own
  business [YEAR]       the VAT terms, tagged business expenses and rules
  points [YEAR]         EuroBonus: points per card, expected against received,
                        points missed by paying with another card, level points
  earn-rule CARD --from DATE --nok R --foreign R [--cap KR] [--shift DAYS]
                        a new earn rule for a card (amex, dnb, sas) from DATE
  points-exclude PATTERN [--note]
                        payments that earn no points on any card (a regex
                        on the lower-cased bank text)
  points-exclude-delete ID
  tag ID [--share 0.5] [--vat-rate 25|15|12|0 | --vat KR] [--note TEXT]
                        payment ID is a business expense (a share of it)
  untag ID              undo tag
  business-rule PATTERN [--share] [--vat-rate] [--note]
                        tag every payment whose payee or text matches
  business-rule-delete RULE
  set KEY VALUE         a setting for the tax estimate: vat_registered_from
                        (YYYY-MM-DD), loan_interest_person (a name),
                        loan_interest_share (0-1: that person's share of the
                        household's loan interest; the rest is the spouse's)
  tax [YEAR]            the income tax estimate
  trumf-session         store the trumf.no login: paste the Cookie header
                        (it is not shown, and may be longer than a line)
  trumf [--fetch]       Trumf purchases per month and store chain, receipts
                        fetched, and the login's state; --fetch: fetch now

Run inside the worker container:
  sudo docker compose exec worker python -m heimkunnskap.manual find "holding as"

Every change re-runs transfer pairing and the categories at once.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import date, timedelta

from . import categories, db, pairing, trumf

# ordinary: income or spending whatever the rules say (stored without a kind)
KINDS = ("saving", "loan", "reimbursement", "ordinary")


def _kr(value) -> str:
    return "" if value is None else f"{value:,.2f}".replace(",", " ")


MONEY = {"bonus", "amount", "lent", "repaid", "interest", "outstanding", "vat", "output", "input", "paid"}


def _table(rows: list, columns: list[str]) -> str:
    if not rows:
        return "(none)"
    cells = [[str(c) for c in columns]] + [
        [_kr(r[c]) if c in MONEY else "" if r[c] is None else str(r[c]) for c in columns]
        for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    return "\n".join("  ".join(v.rjust(w) if columns[i] in MONEY | {"id"}
                               else v.ljust(w) for i, (v, w) in enumerate(zip(row, widths))).rstrip()
                     for row in cells)


async def find(conn, text: str, start: date | None, end: date | None) -> str:
    rows = await conn.fetch(
        """SELECT id, booking_date, account, amount, left(description, 60) AS description,
                  coalesce(kind, '') || CASE WHEN rule IS NOT NULL THEN ' (' || rule || ')' ELSE '' END
                      AS classified
           FROM report.transactions
           WHERE (description ILIKE '%' || $1 || '%' OR counterparty ILIKE '%' || $1 || '%')
             AND ($2::date IS NULL OR booking_date >= $2)
             AND ($3::date IS NULL OR booking_date <= $3)
           ORDER BY booking_date, id LIMIT 200""", text, start, end)
    return _table(rows, ["id", "booking_date", "account", "amount", "description", "classified"])


async def _txn(conn, txn_id: int):
    row = await conn.fetchrow("SELECT id, amount_minor, booking_date, description FROM txn "
                              "WHERE id = $1", txn_id)
    if row is None:
        raise SystemExit(f"no transaction with id {txn_id}")
    return row


async def lend(conn, txn_id: int, repaid_by: str, note: str | None,
               until: date | None = None) -> str:
    t = await _txn(conn, txn_id)
    if t["amount_minor"] >= 0:
        raise SystemExit("a loan is money going out: that transaction is money in")
    problem = categories.check_pattern(repaid_by)
    if problem:
        raise SystemExit(f"--repaid-by: {problem}")
    if await conn.fetchval("SELECT manual FROM txn_class WHERE txn_id = $1", txn_id):
        raise SystemExit("that transaction is marked already: unmark it first")
    await conn.execute(
        """INSERT INTO lending (txn_id, repaid_by, note, until) VALUES ($1, $2, $3, $4)
           ON CONFLICT (txn_id) DO UPDATE SET repaid_by = excluded.repaid_by,
                                              note = excluded.note, until = excluded.until""",
        txn_id, repaid_by, note, until)
    await pairing.run(conn)
    rows = await conn.fetch("SELECT * FROM report.lending ORDER BY lent_on")
    repaid = await conn.fetch(
        """SELECT t.id, t.booking_date, round(t.amount_minor / 100.0, 2) AS amount,
                  round(c.principal_minor / 100.0, 2) AS repaid,
                  round(c.interest_minor / 100.0, 2) AS interest
           FROM txn_class c JOIN txn t ON t.id = c.txn_id
           WHERE c.kind = 'lending' AND c.partner_txn_id = $1 ORDER BY t.booking_date, t.id""",
        txn_id)
    return (_table(rows, ["id", "lent_on", "lent", "repaid", "interest", "outstanding",
                          "last_repayment", "note"])
            + "\n\nRepayments of this loan:\n"
            + _table(repaid, ["id", "booking_date", "amount", "repaid", "interest"]))


async def mark(conn, txn_ids: int | list[int], kind: str) -> str:
    """Mark one or more payments by hand. All ids are checked before any
    is changed; the classification is recomputed once at the end."""
    ids = [txn_ids] if isinstance(txn_ids, int) else list(dict.fromkeys(txn_ids))
    found = [await _txn(conn, i) for i in ids]
    for t in found:
        if await conn.fetchval("SELECT 1 FROM lending WHERE txn_id = $1", t["id"]):
            raise SystemExit(f"transaction {t['id']} is a loan made: unmark it first")
    stored = None if kind == "ordinary" else kind
    async with conn.transaction():
        for t in found:
            principal = t["amount_minor"] if kind == "loan" else None
            await conn.execute("DELETE FROM txn_class WHERE txn_id = $1", t["id"])
            await conn.execute(
                """INSERT INTO txn_class (txn_id, kind, rule, manual, principal_minor,
                                          interest_minor, fees_minor)
                   VALUES ($1, $2, $3, true, $4, $5, $5)""",
                t["id"], stored, f"manual:{kind}", principal, 0 if kind == "loan" else None)
    await pairing.run(conn)
    return "\n".join(f"{t['booking_date']} {_kr(t['amount_minor'] / 100)} {t['description']}: {kind}"
                     for t in found)


async def unmark(conn, txn_id: int) -> str:
    await _txn(conn, txn_id)
    done = await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM lending WHERE txn_id = $1)
               OR EXISTS (SELECT 1 FROM txn_class WHERE txn_id = $1 AND manual)""", txn_id)
    if not done:
        return "nothing to undo for that transaction"
    async with conn.transaction():
        await conn.execute("DELETE FROM lending WHERE txn_id = $1", txn_id)
        await conn.execute("DELETE FROM txn_class WHERE txn_id = $1 AND manual", txn_id)
    await pairing.run(conn)
    return "undone"


async def split_loan(conn, txn_id: int, principal: float, interest: float, fees: float) -> str:
    t = await _txn(conn, txn_id)
    if t["amount_minor"] >= 0:
        raise SystemExit("a loan payment is money going out: that transaction is money in")
    if min(principal, interest, fees) < 0:
        raise SystemExit("principal, interest and fees are positive amounts")
    parts = [round(principal * 100), round(interest * 100), round(fees * 100)]
    if sum(parts) != -t["amount_minor"]:
        raise SystemExit(f"{_kr(principal)} + {_kr(interest)} + {_kr(fees)} = "
                         f"{_kr(sum(parts) / 100)}, but the payment is {_kr(-t['amount_minor'] / 100)}")
    if await conn.fetchval("SELECT 1 FROM lending WHERE txn_id = $1", txn_id):
        raise SystemExit("that transaction is a loan made: unmark it first")
    async with conn.transaction():
        await conn.execute("DELETE FROM txn_class WHERE txn_id = $1", txn_id)
        await conn.execute(
            """INSERT INTO txn_class (txn_id, kind, rule, manual, principal_minor,
                                      interest_minor, fees_minor)
               VALUES ($1, 'loan', 'manual:loan_split', true, $2, $3, $4)""",
            txn_id, -parts[0], -parts[1], -parts[2])
    await pairing.run(conn)
    return (f"{t['booking_date']} {_kr(t['amount_minor'] / 100)}: principal {_kr(principal)}, "
            f"interest {_kr(interest)}, fees {_kr(fees)}")


async def credit_line_gaps(conn, today: date | None = None) -> list:
    """Months with a direct payment to the credit line (rammelån) that the
    rules took for principal, and no interest-only debit for it. The bank
    debits the month's interest from an account around the 20th; if it
    is missing, the interest was taken out of the payment itself."""
    today = today or date.today()
    return await conn.fetch(
        """SELECT date_trunc('month', booking_date)::date AS month,
                  count(*) AS payments, round(-sum(amount) / 100.0, 2) AS total,
                  string_agg(id::text, ', ' ORDER BY id) AS ids
           FROM (SELECT t.id, t.booking_date, t.amount_minor AS amount
                 FROM txn t JOIN txn_class c ON c.txn_id = t.id
                 WHERE c.rule = 'loan:credit_line' AND NOT c.manual) p
           WHERE NOT EXISTS (
                   SELECT 1 FROM txn t JOIN txn_class c ON c.txn_id = t.id
                   WHERE c.rule = 'loan:instalment' AND c.principal_minor = 0
                     AND c.interest_minor < 0
                     AND date_trunc('month', t.booking_date) = date_trunc('month', p.booking_date))
             AND date_trunc('month', booking_date) + interval '24 days' < $1::date
           GROUP BY 1 ORDER BY 1""", today)


async def check(conn) -> str:
    gaps = await credit_line_gaps(conn)
    if not gaps:
        return "No month looks like interest was netted out of a payment to the rammelån."
    return ("Months with a direct payment to the rammelån and no interest debit for it. The bank "
            "probably took the interest out of the payment: find the split on the loan's "
            "statement in the DNB app and run manual split ID --principal KR --interest KR "
            "--fees KR for each payment.\n"
            + _table([{**dict(g), "amount": g["total"]} for g in gaps],
                     ["month", "payments", "amount", "ids"]))


async def claims_report(conn) -> str:
    from . import claims as claims_mod
    rows = await conn.fetch("SELECT * FROM report.expense_claims ORDER BY trip_from, id")
    if not rows:
        return "No expense claims yet: upload or email the PDFs."
    out = []
    for c in rows:
        percents = [float(r["tax_percent"]) for r in await conn.fetch(
            """SELECT DISTINCT s.tax_percent FROM payslip s JOIN person p ON p.id = s.person_id
               WHERE p.name = $1 AND s.tax_percent IS NOT NULL""", c["person"])]
        expect = claims_mod.expected_payouts(round(c["total"] * 100), round(c["taxable"] * 100),
                                             percents or [0])
        out.append(f"Claim {c['claim_no']}, {c['person']}: {c['purpose'] or c['title']}, "
                   f"{c['trip_from']:%d.%m}-{c['trip_to']:%d.%m.%Y}. Total {_kr(c['total'])}, "
                   f"taxable {_kr(c['taxable'])}, outlays {_kr(c['outlays'])}")
        out.append(f"  paid: {_kr(c['paid'])} on {c['paid_on']}" if c["paid_on"] else
                   "  paid: not found; expected a deposit of "
                   + " or ".join(_kr(a / 100) for a in expect)
                   + " (claim-payout NO TXN if it was paid with another claim)")
        lines = await conn.fetch(
            """SELECT id, kind, day, amount, currency, currency_amount, note, match, txn_id,
                      paid_on, paid, left(paid_text, 40) AS paid_text
               FROM report.expense_claim_lines WHERE claim_no = $1 AND section = 'cost'
                 AND kind <> ALL('{mileage,allowance,per_diem,deduction}'::text[]) AND amount <> 0
               ORDER BY line_no""", c["claim_no"])
        for l in lines:
            what = (f"{l['currency']} {_kr(l['currency_amount'])} = " if l["currency"] else "") \
                   + _kr(l["amount"])
            found = (f"-> {l['txn_id']} {l['paid_on']} {_kr(l['paid'])} {l['paid_text']}"
                     + (" (a guess: other payments fit too)" if l["match"] == "ambiguous" else "")
                     + (" (set by hand)" if l["match"] == "manual" else "")
                     if l["txn_id"] else
                     "-> not paid by card" if l["match"] == "none" else "-> no card payment found")
            out.append(f"  line {l['id']}: {l['day']} {l['kind']} {what} {l['note'][:30]} {found}")
        out.append("")
    return "\n".join(out).rstrip()


async def claim_line(conn, line_id: int, target: str) -> str:
    from . import claims as claims_mod
    line = await conn.fetchrow("SELECT id, claim_id FROM expense_claim_line WHERE id = $1", line_id)
    if line is None:
        raise SystemExit(f"no claim line {line_id} (see: manual claims)")
    if target == "auto":
        await conn.execute("UPDATE expense_claim_line SET txn_id = NULL, match = NULL WHERE id = $1",
                           line_id)
    elif target == "none":
        await conn.execute("UPDATE expense_claim_line SET txn_id = NULL, match = 'none' "
                           "WHERE id = $1", line_id)
    else:
        t = await _txn(conn, int(target))
        if t["amount_minor"] >= 0:
            raise SystemExit("an outlay is money going out: that transaction is money in")
        await conn.execute("UPDATE expense_claim_line SET txn_id = NULL, match = NULL "
                           "WHERE txn_id = $1 AND id <> $2", t["id"], line_id)
        await conn.execute("UPDATE expense_claim_line SET txn_id = $2, match = 'manual' "
                           "WHERE id = $1", line_id, t["id"])
    await claims_mod.run(conn)
    return await claims_report(conn)


async def claim_payout(conn, claim_no: str, target: str) -> str:
    from . import claims as claims_mod
    claim = await conn.fetchrow("SELECT id FROM expense_claim WHERE claim_no = $1", claim_no)
    if claim is None:
        raise SystemExit(f"no expense claim {claim_no} (see: manual claims)")
    if target == "auto":
        await conn.execute("UPDATE expense_claim SET payout_txn_id = NULL, payout_manual = false "
                           "WHERE id = $1", claim["id"])
    elif target == "none":
        await conn.execute("UPDATE expense_claim SET payout_txn_id = NULL, payout_manual = true "
                           "WHERE id = $1", claim["id"])
    else:
        t = await _txn(conn, int(target))
        if t["amount_minor"] <= 0:
            raise SystemExit("a payout is money coming in: that transaction is money out")
        if await conn.fetchval("SELECT kind FROM txn_class WHERE txn_id = $1", t["id"]):
            raise SystemExit("that deposit is classified already (a transfer, loan or mark)")
        await conn.execute("UPDATE expense_claim SET payout_txn_id = $2, payout_manual = true "
                           "WHERE id = $1", claim["id"], t["id"])
    await claims_mod.run(conn)
    return await claims_report(conn)


async def business_report(conn, year: int) -> str:
    from . import business
    out = []
    terms = await business.terms(conn)
    if not terms:
        out.append("VAT terms: none (set vat_registered_from first)")
    else:
        out.append("VAT terms (kroner):")
        rows = [{"term": t["name"], "due": t["due"], "output": t["output_vat"],
                 "input": t["input_vat"], "paid": t["paid"], "outstanding": t["outstanding"],
                 "status": t["status"]} for t in terms]
        out.append(_table(rows, ["term", "due", "output", "input", "paid", "outstanding", "status"]))
    expenses = await conn.fetch(
        """SELECT id, booking_date, cost AS amount, input_vat AS vat, share, vat_rate, source,
                  left(description, 45) AS description
           FROM report.business_expenses WHERE extract(year FROM booking_date) = $1
           ORDER BY booking_date, id""", year)
    out.append(f"\nBusiness expenses {year} (amount incl. VAT, then input VAT):")
    out.append(_table(expenses, ["id", "booking_date", "amount", "vat", "share", "vat_rate",
                                 "source", "description"]))
    rules = await conn.fetch("SELECT id, pattern, share, vat_rate, note FROM business_rule ORDER BY id")
    out.append("\nRules:")
    out.append(_table(rules, ["id", "pattern", "share", "vat_rate", "note"]))
    return "\n".join(out)


async def tag_expense(conn, txn_id: int, share: float, vat_rate: float, vat: float | None,
                      note: str | None) -> str:
    from . import business
    try:
        return await business.tag(conn, txn_id, share, vat_rate, vat, note or "")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


async def untag_expense(conn, txn_id: int) -> str:
    from . import business
    try:
        return await business.untag(conn, txn_id)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


async def business_rule(conn, pattern: str, share: float, vat_rate: float, note: str | None) -> str:
    from . import business
    try:
        rid = await business.add_rule(conn, pattern, share, vat_rate, note or "")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    n = await conn.fetchval("SELECT count(*) FROM business_expense WHERE rule_id = $1", rid)
    return f"rule {rid}: {n} payments tagged"


async def business_rule_delete(conn, rule_id: int) -> str:
    from . import business
    await business.delete_rule(conn, rule_id)
    return f"rule {rule_id} deleted; its tags are gone"


async def points_report(conn, year: int) -> str:
    from . import points
    await points.run(conn)
    out = []
    totals = await conn.fetch(
        """SELECT CASE WHEN partner ~ ' - ' THEN 'Flights flown' ELSE partner END AS source,
                  sum(bonus_points) AS earned, sum(level_points) AS level
           FROM eb_txn WHERE extract(year FROM day) = $1 AND flow IN ('earned', 'level')
           GROUP BY 1 ORDER BY sum(bonus_points) DESC NULLS LAST, 1""", year)
    out.append(f"EuroBonus {year}, points earned by source (award refunds left out):")
    out.append(_table(totals, ["source", "earned", "level"]) if totals else
               "(none: upload the EuroBonus export)")
    awards = await conn.fetch(
        """SELECT day, partner, -bonus_points AS points FROM eb_txn
           WHERE flow = 'award' AND extract(year FROM day) = $1 ORDER BY day""", year)
    changed = await conn.fetchrow(
        """SELECT count(*) AS n, coalesce(-sum(bonus_points), 0) AS points FROM eb_txn
           WHERE flow = 'award_changed' AND extract(year FROM day) = $1""", year)
    out.append(f"\nAward travel kept: {sum(a['points'] for a in awards):,} points on "
               f"{len(awards)} bookings; {changed['n']} more ({changed['points']:,} points) changed "
               "or cancelled and refunded".replace(",", " "))
    out.append(_table(awards, ["day", "partner", "points"]))
    level12 = await conn.fetchval("SELECT coalesce(sum(level_points), 0) FROM eb_txn "
                                  "WHERE day > current_date - interval '12 months'")
    out.append(f"Level points: {sum(t['level'] or 0 for t in totals):,} in {year}, "
               f"{level12:,} in the last 12 months".replace(",", " "))
    monthly = await conn.fetch(
        """SELECT c.label AS card, m.period, m.earning_spend AS amount, m.expected, m.received,
                  m.difference, m.missed
           FROM report.points_monthly m JOIN points_card c ON c.code = m.card
           WHERE extract(year FROM m.period) = $1 ORDER BY c.code, m.period""", year)
    out.append("\nPer card and posting month (amount: point-earning spend, kr):")
    out.append(_table([{**dict(r), "period": f"{r['period']:%Y-%m}"} for r in monthly],
                      ["card", "period", "amount", "expected", "received", "difference", "missed"]))
    missed = await conn.fetch(
        """SELECT merchant, card, best_card, count(*) AS purchases, round(sum(missed)) AS missed
           FROM report.points_purchases WHERE extract(year FROM booking_date) = $1 AND missed > 0
           GROUP BY merchant, card, best_card ORDER BY sum(missed) DESC LIMIT 15""", year)
    total_missed = await conn.fetchval(
        "SELECT coalesce(round(sum(missed)), 0) FROM report.points_purchases "
        "WHERE extract(year FROM booking_date) = $1", year)
    out.append(f"\nMissed: {int(total_missed):,} points by paying with another card than the best "
               "(Amex counted only where it has been used). Largest:".replace(",", " "))
    out.append(_table(missed, ["merchant", "card", "best_card", "purchases", "missed"]))
    excluded = await conn.fetch(
        """SELECT excluded, count(*) AS purchases, round(sum(-amount)) AS amount
           FROM report.points_purchases WHERE extract(year FROM booking_date) = $1
             AND excluded IS NOT NULL GROUP BY 1 ORDER BY 3 DESC""", year)
    out.append("\nEarning nothing:")
    out.append(_table(excluded, ["excluded", "purchases", "amount"]))
    rules = await conn.fetch(
        """SELECT r.card, r.valid_from, r.valid_to, r.rate_nok, r.rate_foreign,
                  r.yearly_cap_minor / 100 AS cap, r.period_shift_days AS shift
           FROM earn_rule r ORDER BY r.card, r.valid_from""")
    out.append("\nEarn rules (points per 100 kr):")
    out.append(_table(rules, ["card", "valid_from", "valid_to", "rate_nok", "rate_foreign", "cap", "shift"]))
    ex = await conn.fetch("SELECT id, note, pattern FROM earn_exclusion ORDER BY id")
    out.append("\nExclusions:")
    out.append(_table(ex, ["id", "note", "pattern"]))
    return "\n".join(out)


async def earn_rule(conn, card: str, valid_from: date, nok: float, foreign: float,
                    cap: float | None, shift: int | None) -> str:
    from . import points
    if not await conn.fetchval("SELECT 1 FROM points_card WHERE code = $1", card):
        raise SystemExit(f"no card {card!r}: amex, dnb or sas")
    prev = await conn.fetchrow(
        "SELECT * FROM earn_rule WHERE card = $1 AND valid_from < $2 ORDER BY valid_from DESC LIMIT 1",
        card, valid_from)
    async with conn.transaction():
        if prev:
            await conn.execute("UPDATE earn_rule SET valid_to = $2 WHERE id = $1",
                               prev["id"], valid_from - timedelta(days=1))
        await conn.execute(
            """INSERT INTO earn_rule (card, valid_from, rate_nok, rate_foreign, yearly_cap_minor,
                                      period_shift_days, note)
               VALUES ($1, $2, $3, $4, $5, $6, 'manual earn-rule')""",
            card, valid_from, nok, foreign, None if cap is None else round(cap * 100),
            shift if shift is not None else (prev["period_shift_days"] if prev else 0))
    await points.run(conn)
    return f"{card}: {nok:g} points per 100 kr, {foreign:g} in foreign currency, from {valid_from}"


async def points_exclude(conn, pattern: str, note: str | None) -> str:
    from . import points
    problem = categories.check_pattern(pattern)
    if problem:
        raise SystemExit(problem)
    eid = await conn.fetchval("INSERT INTO earn_exclusion (pattern, note) VALUES ($1, $2) RETURNING id",
                              pattern, note or "")
    await points.run(conn)
    n = await conn.fetchval("SELECT count(*) FROM points_expected WHERE excluded = $1", note or "excluded")
    return f"exclusion {eid}: {n} payments earn nothing"


async def points_exclude_delete(conn, eid: int) -> str:
    from . import points
    await conn.execute("DELETE FROM earn_exclusion WHERE id = $1", eid)
    await points.run(conn)
    return f"exclusion {eid} deleted"


SETTINGS = ("vat_registered_from", "loan_interest_person", "loan_interest_share")


async def set_vat(conn, txn_id: int, kr: float, note: str | None, term: str | None = None) -> str:
    t = await _txn(conn, txn_id)
    if t["amount_minor"] <= 0:
        raise SystemExit("VAT is set on money in: that transaction is money out")
    vat = round(kr * 100)
    if vat > t["amount_minor"]:
        raise SystemExit("the VAT cannot be more than the deposit")
    term_day = None
    if term:
        try:
            term_day = date.fromisoformat(term + "-01" if len(term) == 7 else term)
        except ValueError:
            raise SystemExit("--term: a month, YYYY-MM") from None
    await conn.execute(
        """INSERT INTO business_vat (txn_id, vat_minor, note, term) VALUES ($1, $2, $3, $4)
           ON CONFLICT (txn_id) DO UPDATE SET vat_minor = excluded.vat_minor, note = excluded.note,
                                              term = excluded.term""",
        txn_id, vat, note, term_day)
    from . import tax
    await tax.refresh(conn)
    return (f"{t['booking_date']} {_kr(t['amount_minor'] / 100)}: VAT {_kr(vat / 100)}, "
            f"income {_kr((t['amount_minor'] - vat) / 100)}")


async def set_value(conn, key: str, value: str) -> str:
    if key == "vat_registered_from":
        try:
            date.fromisoformat(value)
        except ValueError:
            raise SystemExit("vat_registered_from: a date, YYYY-MM-DD") from None
    if key == "loan_interest_share":
        try:
            ok = 0 <= float(value) <= 1
        except ValueError:
            ok = False
        if not ok:
            raise SystemExit("loan_interest_share: a number from 0 to 1, e.g. 0.5")
    await db.set_setting(conn, key, value)
    from . import tax
    await tax.refresh(conn)
    return f"{key} = {value}"


async def tax_report(conn, year: int) -> str:
    from . import tax
    result = await tax.estimate(conn, year)
    if "error" in result:
        return result["error"]
    out = [f"Income tax {year}, estimated {result['as_of']} (kroner)"]
    rows = [("Salary and benefits", "salary"), ("Business profit", "business_profit"),
            ("Dividends", "dividends"), ("Minimum deduction", "minimum_deduction"),
            ("Pension contributions", "pension_deduction"), ("Loan interest", "interest_deduction"),
            ("Gifts, fees", "other_deduction"), ("General income", "general_income"),
            ("Tax on general income (22%)", "general_tax"),
            ("Bracket tax", "bracket_tax"), ("Social security", "social_security"),
            ("Total tax", "total_tax"), ("Withheld (projected)", "withheld"),
            ("Advance tax paid", "prepaid"), ("Refund (+) or to pay (-)", "balance")]
    people = result["people"]
    if not people:
        return f"No pay slips for {year}: upload them first."
    width = max(len(p["person"]) for p in people) + 2
    out.append("".ljust(30) + "".join(p["person"].rjust(max(width, 16)) for p in people))
    for label, key in rows:
        out.append(label.ljust(30) + "".join(_kr(round(p[key])).rjust(max(width, 16)) for p in people))
    for p in people:
        out.append(f"\n{p['person']}:")
        out += [f"  - {n}" for n in p["notes"]]
    return "\n".join(out)


def read_secret(prompt: str) -> str:
    """A line from the terminal without echo, and without the terminal's
    4 KB line limit (a Trumf session cookie can be longer). From a pipe:
    the whole of stdin."""
    if not sys.stdin.isatty():
        return sys.stdin.read()
    import termios
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ICANON | termios.ECHO)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    chars = []
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        while True:
            c = os.read(fd, 4096).decode(errors="replace")
            if not c:
                break
            if "\n" in c or "\r" in c:
                chars.append(re.split(r"[\r\n]", c, maxsplit=1)[0])
                break
            chars.append(c)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stderr.write("\n")
    return "".join(chars)


async def trumf_session(conn, text: str) -> str:
    try:
        n = await trumf.set_session(conn, text)
    except ValueError as exc:
        return f"Nothing stored: {exc}."
    return (f"Stored the trumf.no login ({n} cookie piece{'s' if n > 1 else ''}). "
            "Fetch now with: manual trumf --fetch")


async def trumf_report(conn, fetch: bool) -> str:
    out = []
    if fetch:
        todo = await conn.fetchval("SELECT count(*) FROM trumf_txn WHERE has_receipt "
                                   "AND receipt_fetched_at IS NULL") if await conn.fetchval(
            "SELECT 1 FROM trumf_session") else 0
        print(f"Fetching from trumf.no, about a second a receipt"
              f"{f' ({todo} known receipts to read)' if todo else ''}...", file=sys.stderr, flush=True)
        result = await trumf.run(conn, max_receipts=100_000)
        out.append("Fetch: " + ", ".join(f"{k} {v}" for k, v in result.items()) + "\n")
    s = await conn.fetchrow("SELECT updated_at, last_ok_at, last_error FROM trumf_session WHERE id = 1")
    if s is None:
        out.append("No trumf.no login stored (run: manual trumf-session).")
    else:
        out.append(f"Login cookie last saved {s['updated_at']:%Y-%m-%d %H:%M} (Trumf renews it); last good fetch "
                   f"{s['last_ok_at']:%Y-%m-%d %H:%M}" if s["last_ok_at"] else
                   f"Login cookie last saved {s['updated_at']:%Y-%m-%d %H:%M} (Trumf renews it); no good fetch yet")
        if s["last_error"]:
            out.append(f"Last error: {s['last_error']}")
    months = await conn.fetch(
        """SELECT to_char(day, 'YYYY-MM') AS month, count(*) AS purchases,
                  sum(amount) AS amount, sum(bonus) AS bonus,
                  count(*) FILTER (WHERE has_receipt AND lines = 0) AS receipts_missing,
                  count(*) FILTER (WHERE txn_id IS NULL) AS unmatched
           FROM report.trumf_purchases WHERE category = 'ACCRUAL' AND grocery
           GROUP BY 1 ORDER BY 1""")
    chains = await conn.fetch(
        """SELECT partner, partner_name AS name, CASE WHEN grocery THEN 'yes' ELSE 'no' END AS grocery,
                  count(*) AS purchases, sum(amount) AS amount, sum(bonus) AS bonus
           FROM report.trumf_purchases WHERE category = 'ACCRUAL'
           GROUP BY 1, 2, 3 ORDER BY grocery DESC, sum(amount) DESC""")
    used = await conn.fetchval(
        "SELECT -sum(bonus) FROM report.trumf_purchases WHERE category = 'CONSUME'")
    out.append("\nGrocery purchases per month (unmatched: no card payment found):\n"
               + _table(months, ["month", "purchases", "amount", "bonus", "receipts_missing",
                                 "unmatched"]))
    out.append("\nPer Trumf partner (only grocery stores in the months above and on the Groceries page):\n"
               + _table(chains, ["partner", "name", "grocery", "purchases", "amount", "bonus"]))
    if used:
        out.append(f"\nTrumf bonus moved out (to EuroBonus): {_kr(used)} kr")
    return "\n".join(out)


async def listing(conn) -> str:
    loans = await conn.fetch("SELECT * FROM report.lending ORDER BY lent_on")
    marks = await conn.fetch(
        """SELECT t.id, t.booking_date, round(t.amount_minor / 100.0, 2) AS amount,
                  coalesce(c.kind, 'ordinary') AS kind, left(t.description, 60) AS description
           FROM txn_class c JOIN txn t ON t.id = c.txn_id
           WHERE c.manual ORDER BY t.booking_date, t.id""")
    gaps = await credit_line_gaps(conn)
    warning = (f"\n\nCheck: {len(gaps)} month(s) with a direct payment to the rammelån and no "
               "interest debit (run: manual check)") if gaps else ""
    return ("Loans made:\n" + _table(loans, ["id", "lent_on", "lent", "repaid", "interest",
                                               "outstanding", "last_repayment", "note"])
            + "\n\nMarked by hand:\n" + _table(marks, ["id", "booking_date", "amount", "kind",
                                                      "description"]) + warning)


async def main_async(args) -> str:
    conn = await db.connect()
    try:
        if args.command == "find":
            return await find(conn, args.text, args.start, args.end)
        if args.command == "lend":
            return await lend(conn, args.id, args.repaid_by, args.note, args.until)
        if args.command == "claims":
            return await claims_report(conn)
        if args.command == "claim-line":
            return await claim_line(conn, args.line, args.txn)
        if args.command == "claim-payout":
            return await claim_payout(conn, args.claim, args.txn)
        if args.command == "split":
            return await split_loan(conn, args.id, args.principal, args.interest, args.fees)
        if args.command == "check":
            return await check(conn)
        if args.command == "mark":
            return await mark(conn, args.ids, args.kind)
        if args.command == "vat":
            return await set_vat(conn, args.id, args.kr, args.note, args.term)
        if args.command == "points":
            return await points_report(conn, args.year)
        if args.command == "earn-rule":
            return await earn_rule(conn, args.card, args.valid_from, args.nok, args.foreign,
                                   args.cap, args.shift)
        if args.command == "points-exclude":
            return await points_exclude(conn, args.pattern, args.note)
        if args.command == "points-exclude-delete":
            return await points_exclude_delete(conn, args.id)
        if args.command == "business":
            return await business_report(conn, args.year)
        if args.command == "tag":
            return await tag_expense(conn, args.id, args.share, args.vat_rate, args.vat, args.note)
        if args.command == "untag":
            return await untag_expense(conn, args.id)
        if args.command == "business-rule":
            return await business_rule(conn, args.pattern, args.share, args.vat_rate, args.note)
        if args.command == "business-rule-delete":
            return await business_rule_delete(conn, args.rule)
        if args.command == "set":
            return await set_value(conn, args.key, args.value)
        if args.command == "tax":
            return await tax_report(conn, args.year)
        if args.command == "trumf-session":
            return await trumf_session(conn, read_secret(
                "Paste the Cookie header from a request to www.trumf.no, then Enter: "))
        if args.command == "trumf":
            return await trumf_report(conn, args.fetch)
        if args.command == "unmark":
            return await unmark(conn, args.id)
        return await listing(conn)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m heimkunnskap.manual", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("find", help="list transactions whose text contains TEXT")
    f.add_argument("text")
    f.add_argument("--from", dest="start", type=date.fromisoformat, metavar="YYYY-MM-DD")
    f.add_argument("--to", dest="end", type=date.fromisoformat, metavar="YYYY-MM-DD")
    l = sub.add_parser("lend", help="payment ID was money lent out")
    l.add_argument("id", type=int)
    l.add_argument("--repaid-by", required=True, metavar="REGEX",
                   help="matches the repayments' text; case does not matter")
    l.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                   help="the last payment on the loan; payments up to then after the "
                        "principal is repaid are interest. Without it the loan ends when "
                        "the principal is repaid")
    l.add_argument("--note")
    m = sub.add_parser("mark", help="payments ID... are saving, loan, reimbursement, or "
                                    "ordinary income or spending whatever the rules say")
    m.add_argument("ids", type=int, nargs="+", metavar="ID")
    m.add_argument("kind", choices=KINDS)
    u = sub.add_parser("unmark", help="undo lend or mark")
    u.add_argument("id", type=int)
    sp = sub.add_parser("split", help="a loan payment the bank split differently from its text")
    sp.add_argument("id", type=int)
    sp.add_argument("--principal", type=float, required=True, metavar="KR")
    sp.add_argument("--interest", type=float, required=True, metavar="KR")
    sp.add_argument("--fees", type=float, default=0.0, metavar="KR")
    sub.add_parser("claims", help="expense claims and what they were matched to")
    cl = sub.add_parser("claim-line", help="the card payment an outlay on a claim was")
    cl.add_argument("line", type=int)
    cl.add_argument("txn", help="a transaction id, none or auto")
    cp = sub.add_parser("claim-payout", help="the deposit that paid a claim")
    cp.add_argument("claim", help="the claim's number (ID on the claim)")
    cp.add_argument("txn", help="a transaction id, none or auto")
    sub.add_parser("check", help="months where interest looks netted out of a loan payment")
    sub.add_parser("list", help="every loan and mark made here")
    v = sub.add_parser("vat", help="the VAT in business deposit ID, when it is not 20%%")
    v.add_argument("id", type=int)
    v.add_argument("kr", type=float)
    v.add_argument("--note")
    v.add_argument("--term", metavar="YYYY-MM", help="a month in the VAT term it belongs to")
    pt = sub.add_parser("points", help="EuroBonus points per card, missed points, level points")
    pt.add_argument("year", type=int, nargs="?", default=date.today().year)
    er = sub.add_parser("earn-rule", help="a new earn rule for a card from a date")
    er.add_argument("card", choices=("amex", "dnb", "sas"))
    er.add_argument("--from", dest="valid_from", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    er.add_argument("--nok", type=float, required=True, help="points per 100 kr in NOK")
    er.add_argument("--foreign", type=float, required=True, help="points per 100 kr in foreign currency")
    er.add_argument("--cap", type=float, metavar="KR", help="most point-earning spend per calendar year")
    er.add_argument("--shift", type=int, metavar="DAYS", help="days a purchase moves to its posting month")
    px = sub.add_parser("points-exclude", help="payments that earn no points on any card")
    px.add_argument("pattern")
    px.add_argument("--note")
    pd = sub.add_parser("points-exclude-delete", help="delete an exclusion")
    pd.add_argument("id", type=int)
    b = sub.add_parser("business", help="VAT terms, business expenses and rules")
    b.add_argument("year", type=int, nargs="?", default=date.today().year)
    for name, helptext in (("tag", "payment ID is a business expense"),):
        tg = sub.add_parser(name, help=helptext)
        tg.add_argument("id", type=int)
        tg.add_argument("--share", type=float, default=1.0, help="the business's share, e.g. 0.5")
        tg.add_argument("--vat-rate", type=float, default=25, choices=(25, 15, 12, 0))
        tg.add_argument("--vat", type=float, metavar="KR", help="the input VAT, if not by the rate")
        tg.add_argument("--note")
    ut = sub.add_parser("untag", help="undo tag")
    ut.add_argument("id", type=int)
    br = sub.add_parser("business-rule", help="tag every payment whose payee or text matches")
    br.add_argument("pattern")
    br.add_argument("--share", type=float, default=1.0)
    br.add_argument("--vat-rate", type=float, default=25, choices=(25, 15, 12, 0))
    br.add_argument("--note")
    bd = sub.add_parser("business-rule-delete", help="delete a business rule and its tags")
    bd.add_argument("rule", type=int)
    s = sub.add_parser("set", help="a setting for the tax estimate")
    s.add_argument("key", choices=SETTINGS)
    s.add_argument("value")
    t = sub.add_parser("tax", help="the income tax estimate")
    t.add_argument("year", type=int, nargs="?", default=date.today().year)
    sub.add_parser("trumf-session", help="store the trumf.no login (paste the Cookie header)")
    tr = sub.add_parser("trumf", help="Trumf purchases, receipts and the login's state")
    tr.add_argument("--fetch", action="store_true", help="fetch new purchases and receipts now")
    args = p.parse_args(argv)
    print(asyncio.run(main_async(args)))


if __name__ == "__main__":
    sys.exit(main())
