"""Importing an uploaded card export: raw store first, then the ledger.

The same file uploaded twice is recognised by its checksum and skipped. A
file that has grown since the last upload (the open billing period) adds
only its new rows: Amex rows by their reference, DNB Mastercard rows by
fingerprint. A rejected file is kept in the raw store with the reason, so
it can be re-read once the parser learns its quirk.

A card that also has a bank feed (the SAS EuroBonus Mastercard) is merged
into the feed's account rather than becoming a second card: the file adds
only the rows from before the feed's first transaction, the history the
bank no longer shares. From then on the feed is the record, since its texts
differ from the file's ("Inbetalning", "Innbetaling") and both would count.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import timedelta

from . import claims, db, eurobonus, pairing, payslips, points, tax
from .cardfiles import AMEX, SAS_MASTERCARD, CardFileError, parse
from .normalise import add_fingerprints

log = logging.getLogger("heimkunnskap.fileimport")

BANK_NAMES = {AMEX: "American Express", "dnb_mastercard": "DNB",
              SAS_MASTERCARD: "SAS EuroBonus Mastercard"}
# Cards that also come through Enable Banking, by the bank's name there
FEED_BANKS = {SAS_MASTERCARD: "SAS EuroBonus Mastercard"}
MATCH_DAYS = timedelta(days=3)


def in_feed(rows: list[dict], feed: list[dict]) -> int:
    """How many of rows have a feed row of the same amount within a few
    days, each feed row used once."""
    left = Counter((f["booking_date"], f["amount_minor"]) for f in feed)
    found = 0
    for r in sorted(rows, key=lambda r: r["booking_date"]):
        for shift in sorted(range(-MATCH_DAYS.days, MATCH_DAYS.days + 1), key=abs):
            key = (r["booking_date"] + timedelta(days=shift), r["amount_minor"])
            if left[key]:
                left[key] -= 1
                found += 1
                break
    return found


async def file_account(conn, key: str, name: str, bank: str) -> int:
    return await conn.fetchval(
        """INSERT INTO account (account_key, source, aspsp_name, name, product,
                                cash_account_type, currency)
           VALUES ($1, 'file', $2, $3, $3, 'CARD', 'NOK')
           ON CONFLICT (account_key) DO UPDATE SET active = true, updated_at = now()
           RETURNING id""", key, bank, name)


async def import_file(conn, filename: str, content: bytes, *, run_pairing: bool = True,
                      via: str = "upload") -> dict:
    """One uploaded file (via: upload or mail). Returns a result for the
    upload page: status imported, duplicate or rejected, with counts or
    the reason."""
    digest = hashlib.sha256(content).hexdigest()
    result = {"filename": filename, "sha256": digest[:12]}
    seen = await conn.fetchrow(
        """SELECT b.started_at FROM raw_record r JOIN import_batch b ON b.id = r.batch_id
           WHERE r.kind = 'file' AND r.sha256 = $1 AND b.error IS NULL AND b.finished_at IS NOT NULL
           ORDER BY b.started_at LIMIT 1""", digest)
    if seen:
        return {**result, "status": "duplicate", "uploaded_at": seen["started_at"]}

    if content[:5] == b"%PDF-":
        return await import_pdf(conn, filename, content, result, via)
    if eurobonus.detect(content):
        return await import_eurobonus(conn, filename, content, result, via)

    try:
        card = parse(content)
    except CardFileError as exc:
        batch = await db.start_batch(conn, source="upload", kind=via)
        await db.store_raw(conn, batch, "file", content=content, filename=filename)
        await db.finish_batch(conn, batch, error=str(exc)[:500])
        log.warning("upload %s rejected: %s", filename, exc)
        return {**result, "status": "rejected", "reason": str(exc)}

    feeds = []
    if card.kind in FEED_BANKS:
        feeds = await conn.fetch(
            """SELECT a.id, a.account_key, min(t.booking_date) AS first
               FROM account a LEFT JOIN txn t ON t.account_id = a.id
               WHERE a.source = 'enable_banking' AND a.aspsp_name = $1
               GROUP BY a.id, a.account_key""", FEED_BANKS[card.kind])
    if len(feeds) > 1:
        reason = (f"more than one {FEED_BANKS[card.kind]} account comes from the bank, so the "
                  "file cannot be matched to one of them")
        batch = await db.start_batch(conn, source="upload", kind=via)
        await db.store_raw(conn, batch, "file", content=content, filename=filename)
        await db.finish_batch(conn, batch, error=reason)
        return {**result, "status": "rejected", "reason": reason}
    rows, checks = card.rows, list(card.checks)
    if feeds:
        feed = feeds[0]
        account_id, card.account_key = feed["id"], feed["account_key"]
        add_fingerprints(card.account_key, card.rows)
        if feed["first"] is not None:
            rows = [r for r in card.rows if r["booking_date"] < feed["first"]]
            later = [r for r in card.rows if r["booking_date"] >= feed["first"]]
            if later:
                feed_rows = [dict(r) for r in await conn.fetch(
                    "SELECT booking_date, amount_minor FROM txn WHERE account_id = $1",
                    account_id)]
                found = in_feed(later, feed_rows)
                checks.append(f"merged with the bank feed, which starts {feed['first']:%d.%m.%Y}: "
                              f"{found} of the {len(later)} rows from then on are in the feed")
                if found < len(later):
                    checks.append(f"{len(later) - found} rows from the feed's period are not in "
                                  "the feed and were not added")
    else:
        account_id = await file_account(conn, card.account_key, card.account_name,
                                        BANK_NAMES[card.kind])
    first = min(r["booking_date"] for r in (rows or card.rows))
    batch = await db.start_batch(conn, source=card.kind, kind=via, account_id=account_id,
                                 date_from=first)
    raw_id = await db.store_raw(conn, batch, "file", content=content, filename=filename)
    for row in card.rows:
        row["raw_record_id"] = raw_id
    if not feeds:
        add_fingerprints(card.account_key, card.rows)
    added = await db.insert_transactions(conn, account_id, batch, rows)
    await db.finish_batch(conn, batch, pages=1, seen=len(card.rows), added=added)
    log.info("upload %s: %s, %d rows, %d new", filename, card.kind, len(card.rows), added)
    if run_pairing:
        await pairing.run(conn)
    return {**result, "status": "imported", "kind": card.kind, "account": card.account_name,
            "period": card.period, "rows": len(card.rows), "added": added,
            "checks": checks}


# -- The EuroBonus account's history

async def import_eurobonus(conn, filename: str, content: bytes, result: dict, via: str) -> dict:
    """The EuroBonus export: every row not seen before is added."""
    try:
        rows = eurobonus.parse(content)
    except eurobonus.EuroBonusError as exc:
        return await _reject(conn, filename, content, result, str(exc), via)
    first, last = min(r.day for r in rows), max(r.day for r in rows)
    batch = await db.start_batch(conn, source="eurobonus", kind=via, date_from=first)
    await db.store_raw(conn, batch, "file", content=content, filename=filename)
    before = await conn.fetchval("SELECT count(*) FROM eb_txn")
    await conn.executemany(
        """INSERT INTO eb_txn (day, activity, partner, kind, bonus_points, level_points,
                               fingerprint, batch_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (fingerprint) DO NOTHING""",
        [(r.day, r.activity, r.partner, r.kind, r.bonus_points, r.level_points, r.fingerprint, batch)
         for r in rows])
    added = await conn.fetchval("SELECT count(*) FROM eb_txn") - before
    await db.finish_batch(conn, batch, pages=1, seen=len(rows), added=added)
    await points.classify_eurobonus(conn)
    checks = await points.import_checks(conn)
    log.info("%s %s: EuroBonus, %d rows, %d new", via, filename, len(rows), added)
    return {**result, "status": "imported", "kind": "eurobonus", "account": "EuroBonus",
            "period": f"{first:%d.%m.%Y}-{last:%d.%m.%Y}", "rows": len(rows), "added": added,
            "checks": checks}


# -- PDFs: pay slips and expense claims

async def _reject(conn, filename: str, content: bytes, result: dict, reason: str, via: str) -> dict:
    batch = await db.start_batch(conn, source="upload", kind=via)
    await db.store_raw(conn, batch, "file", content=content, filename=filename)
    await db.finish_batch(conn, batch, error=reason[:500])
    log.warning("%s %s rejected: %s", via, filename, reason)
    return {**result, "status": "rejected", "reason": reason}


def pdf_problem(content: bytes) -> str | None:
    """Why a PDF cannot be read at all, in words the uploader can act on;
    None if it opens."""
    import io
    import pypdf
    if not content.lstrip()[:5] == b"%PDF-":
        start = content.lstrip()[:200].lower()
        kind = ("a web page saved as .pdf" if b"<html" in start or b"<!doctype" in start
                else "empty" if not content.strip() else "not a PDF")
        return f"the file is {kind}: download the pay slip again"
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        if reader.is_encrypted and not reader.decrypt(""):
            return ("the PDF is password-protected: open it with its password and save an "
                    "unprotected copy (docs/deploy.md, Pay slips), then upload that")
        len(reader.pages)
    except Exception as exc:                      # pypdf raises many kinds
        return f"the PDF is damaged ({type(exc).__name__}): download it again"
    return None


async def import_pdf(conn, filename: str, content: bytes, result: dict, via: str) -> dict:
    """A pay slip, or else an expense claim."""
    try:
        slip = payslips.parse(content)
    except payslips.PayslipError as slip_error:
        try:
            claim = claims.parse(content)
        except claims.ClaimError as exc:
            unread = "could not read the PDF" in str(exc) or "not a readable PDF" in str(slip_error)
            problem = pdf_problem(content) if unread else None
            if problem:
                return await _reject(conn, filename, content, result, problem, via)
            if "not an expense claim" in str(exc):
                return await _reject(conn, filename, content, result,
                                     f"{slip_error}; not an expense claim either", via)
            return await _reject(conn, filename, content, result, str(exc), via)
        return await import_claim(conn, filename, content, result, claim, via)
    return await import_payslip(conn, filename, content, result, slip, via)


async def import_claim(conn, filename: str, content: bytes, result: dict,
                       claim: "claims.Claim", via: str) -> dict:
    """An expense claim: the PDF to the raw store, the claim and its lines
    to expense_claim. A claim sent again replaces the earlier copy."""
    pid = await person_id(conn, claim.person_name)
    batch = await db.start_batch(conn, source="expense_claim", kind=via, date_from=claim.trip_from)
    raw_id = await db.store_raw(conn, batch, "file", content=content, filename=filename)
    async with conn.transaction():
        claim_id = await conn.fetchval(
            """INSERT INTO expense_claim (person_id, claim_no, title, purpose, trip_from, trip_to,
                                          total_minor, tax_free_minor, taxable_minor, ocr,
                                          raw_record_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
               ON CONFLICT (person_id, claim_no) DO UPDATE SET
                   title = excluded.title, purpose = excluded.purpose,
                   trip_from = excluded.trip_from, trip_to = excluded.trip_to,
                   total_minor = excluded.total_minor, tax_free_minor = excluded.tax_free_minor,
                   taxable_minor = excluded.taxable_minor, ocr = excluded.ocr,
                   raw_record_id = excluded.raw_record_id
               RETURNING id""",
            pid, claim.claim_no, claim.title, claim.purpose, claim.trip_from, claim.trip_to,
            claim.total, claim.tax_free, claim.taxable, claim.ocr, raw_id)
        await conn.execute("DELETE FROM expense_claim_line WHERE claim_id = $1", claim_id)
        await conn.executemany(
            """INSERT INTO expense_claim_line (claim_id, line_no, section, kind, day, type_text, note,
                                               amount_minor, currency, currency_amount_minor)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
            [(claim_id, n, l.section, l.kind, l.day, l.text, l.note, l.amount, l.currency,
              l.currency_amount) for n, l in enumerate(claim.lines, 1)])
    await db.finish_batch(conn, batch, pages=1, seen=len(claim.lines), added=len(claim.lines))
    await claims.run(conn)
    checks = await claim_checks(conn, claim_id)
    log.info("%s %s: expense %s", via, filename, claim.summary())
    return {**result, "status": "imported", "kind": "expense_claim",
            "account": f"Expense claim {claim.claim_no}, {claim.purpose or claim.title}",
            "period": f"{claim.trip_from:%d.%m}-{claim.trip_to:%d.%m.%Y}",
            "rows": len(claim.lines), "added": len(claim.lines), "checks": checks}


async def claim_checks(conn, claim_id: int) -> list[str]:
    c = await conn.fetchrow("SELECT * FROM report.expense_claims WHERE id = $1", claim_id)
    outlays = await conn.fetch(
        """SELECT kind, amount, match, paid_on, paid FROM report.expense_claim_lines l
           WHERE l.claim_no = $1 AND section = 'cost' AND kind = ANY('{hotel,flight,train,taxi,"""
        """transit,parking,toll,car_hire,fuel,other}'::text[]) AND amount > 0""", c["claim_no"])
    kr = lambda v: f"{v:,.2f}".replace(",", " ")              # noqa: E731
    ocr = await conn.fetchval("SELECT ocr FROM expense_claim WHERE id = $1", claim_id)
    checks = [f"total {kr(c['total'])}, of it taxable {kr(c['taxable'])}"
              + (" (a scan, read by OCR; its lines agree with its totals)" if ocr else "")]
    if c["paid_on"]:
        checks.append(f"paid out {kr(c['paid'])} on {c['paid_on']:%d.%m.%Y}")
    else:
        checks.append("the payout was not found among the deposits after the trip "
                      "(manual claims shows what to look for)")
    matched = [o for o in outlays if o["match"] in ("auto", "ambiguous", "manual")]
    checks.append(f"{len(matched)} of {len(outlays)} outlays found among the card payments")
    for o in outlays:
        if o["match"] == "ambiguous":
            checks.append(f"{o['kind']} {kr(o['amount'])}: matched to {kr(-o['paid'])} on "
                          f"{o['paid_on']:%d.%m}, but other payments fit too (manual claims)")
    return checks

async def person_id(conn, name: str) -> int:
    return await conn.fetchval(
        """INSERT INTO person (name, name_key) VALUES ($1, $2)
           ON CONFLICT (name_key) DO UPDATE SET name = person.name
           RETURNING id""", name, payslips.name_key(name))


async def import_payslip(conn, filename: str, content: bytes, result: dict,
                         slip: "payslips.Payslip", via: str = "upload") -> dict:
    """A pay slip: the PDF to the raw store, its figures to payslip. A slip
    for a period already stored replaces it (a corrected slip)."""
    pid = await person_id(conn, slip.person_name)
    batch = await db.start_batch(conn, source=f"payslip:{slip.layout}", kind=via,
                                 date_from=slip.period)
    raw_id = await db.store_raw(conn, batch, "file", content=content, filename=filename)
    checks = []
    # The net pay should be in the bank within a few days of the pay date
    deposit = await conn.fetchrow(
        """SELECT t.booking_date, coalesce(a.label, a.product, a.name) AS account
           FROM txn t JOIN account a ON a.id = t.account_id
           WHERE t.amount_minor = $1 AND t.booking_date BETWEEN $2::date - 3 AND $2::date + 5
           ORDER BY abs(t.booking_date - $2::date) LIMIT 1""", slip.net_paid, slip.pay_date)
    if deposit:
        checks.append(f"net pay {slip.net_paid / 100:,.2f} is the deposit on "
                      f"{deposit['booking_date']:%d.%m.%Y} into {deposit['account']}".replace(",", " "))
    else:
        checks.append("net pay not found in the bank feed near the pay date")
    # Year-to-date withholding carries on from last month's slip
    prev = await conn.fetchrow(
        """SELECT period, withheld_ytd_minor FROM payslip
           WHERE person_id = $1 AND employer = $2 AND period < $3
           ORDER BY period DESC LIMIT 1""", pid, slip.employer, slip.period)
    if prev and (prev["period"].year, prev["period"].month % 12 + 1) == (slip.period.year, slip.period.month):
        expected = prev["withheld_ytd_minor"] + slip.withheld_period
        checks.append("tax withheld so far agrees with last month's slip" if expected == slip.withheld_ytd
                      else f"tax withheld so far differs from last month's slip plus this month "
                           f"by {(slip.withheld_ytd - expected) / 100:,.2f}".replace(",", " "))
    lines = [{"code": l.code, "text": l.text, "period": l.period, "ytd": l.ytd} for l in slip.lines]
    await conn.execute(
        """INSERT INTO payslip (person_id, employer, employer_orgnr, period, pay_date,
                                gross_ytd_minor, tax_base_ytd_minor, withheld_ytd_minor,
                                pension_ytd_minor, salary_minor, benefits_minor, pension_minor,
                                withholding_minor, tax_table, tax_percent, net_paid_minor,
                                lines, raw_record_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                   $17::jsonb, $18)
           ON CONFLICT (person_id, employer, period) DO UPDATE SET
               pay_date = excluded.pay_date, gross_ytd_minor = excluded.gross_ytd_minor,
               tax_base_ytd_minor = excluded.tax_base_ytd_minor,
               withheld_ytd_minor = excluded.withheld_ytd_minor,
               pension_ytd_minor = excluded.pension_ytd_minor, salary_minor = excluded.salary_minor,
               benefits_minor = excluded.benefits_minor, pension_minor = excluded.pension_minor,
               withholding_minor = excluded.withholding_minor, tax_table = excluded.tax_table,
               tax_percent = excluded.tax_percent, net_paid_minor = excluded.net_paid_minor,
               lines = excluded.lines, raw_record_id = excluded.raw_record_id""",
        pid, slip.employer, slip.employer_orgnr, slip.period, slip.pay_date, slip.gross_ytd,
        slip.tax_base_ytd, slip.withheld_ytd, slip.pension_ytd, slip.salary, slip.benefits,
        slip.pension, slip.withholding, slip.tax_table, slip.tax_percent, slip.net_paid,
        json.dumps(lines), raw_id)
    await db.finish_batch(conn, batch, pages=1, seen=1, added=1)
    await tax.refresh(conn)
    log.info("upload %s: pay slip %s", filename, slip.summary())
    return {**result, "status": "imported", "kind": "payslip",
            "account": f"Pay slip, {slip.employer}, {slip.person_name}",
            "period": f"{slip.period:%m.%Y}", "rows": 1, "added": 1, "checks": checks}
