"""Transfer pairing: which transactions are not income or spending.

  own_transfer  a move between two of the household's accounts, paired by
                opposite amount within a few days. A move whose other side
                is not found stays an own transfer without a partner, unless
                the bank profile says where it went (DNB: the credit-line
                mortgage, which PSD2 does not show).
  card_payment  paying a card bill: the debit on the bank account and, once
                the card's data is in, the matching credit on the card.
  saving        fund purchases, and money back from funds and shares sold,
                which counts as savings taken out rather than income
  reimbursement money paid back for an expense laid out ("utlegg"), or
                support that lowers a cost (electricity support from Elvia):
                it reduces spending rather than counting as income. An
                "utlegg" is paired with the expense of the same amount when
                found
  loan          loan instalments and payouts, and money into or out of a
                credit-line mortgage. Where the bank's text carries the
                split into principal, interest and fees, the bank profile's
                loan_split() reads it.
  lending       money the household has lent out (to a company of its own,
                say), and its repayments. The bank text cannot tell a loan
                from any other payment, so the household names each loan in
                the lending table (python -m heimkunnskap.manual lend);
                lent() finds the repayments. Lending and repayment count as
                saving; what comes back beyond the amount lent is interest,
                which is income.

How each bank writes these is in its profile (heimkunnskap/banks/); this
module holds the pairing and the rules shared by all banks. The
classification is recomputed from scratch after every sync and upload;
rows marked manual are left alone. Pure logic in classify(); run() reads
and writes the database.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import timedelta

from . import banks, categories, tax

log = logging.getLogger("heimkunnskap.pairing")


def _name_key(name: str | None) -> frozenset:
    """A person's name as a set of lower-case words: a bank may write the
    holder "Ola Eksempel Nordmann" on the account and "Eksempel Ola
    Nordmann" in transfers."""
    return frozenset((name or "").lower().split())


OWN_WINDOW = timedelta(days=3)

# The card accounts, by brand: a bill paid from a bank (the bank profile's
# card_bills) is paired with a credit on the card account of that brand.
CARDS = {
    "amex": lambda a: (a["account_key"] or "").startswith("file:amex:"),
    "sas": lambda a: a["aspsp_name"] == "SAS EuroBonus Mastercard",
    "dnb_mastercard": lambda a: a["account_key"] == "file:dnb_mastercard",
    # Trumf Visa: no feed and no export, so its bills stand for its
    # purchases (no detail)
    "trumf": lambda a: a["account_key"] == "file:trumf",
}
# The card side: a bill paid can arrive a little before or after the debit
CARD_BEFORE, CARD_AFTER = timedelta(days=3), timedelta(days=10)
# The card's own text for a bill paid (Amex, DNB Mastercard, SEB Kort)
CARD_CREDIT_TEXT = re.compile(r"BETALING MOTTATT|Inbetalning|Innbetaling", re.I)

# Rules for every bank: what a bank profile does not hold

REIMBURSEMENT = re.compile(r"\butlegg\b", re.I)
REIMBURSEMENT_WINDOW = timedelta(days=90)    # the expense comes first
# Money in that lowers a cost rather than being income: the electricity
# support the grid company (Elvia) pays out
COST_REDUCTION = [("reimbursement:electricity_support", re.compile(r"\belvia\b", re.I))]
# Shares bought through an employer's share programme
SAVING = banks.rules(("saving:employee_shares", r"\bEmployee Invest\b"))
# Student loan instalments (Lånekassen); the text does not split interest out
LOANS = banks.rules(("loan:student", r"\bL[åa}]nekasse"))


def _closest(candidates: list[dict], when, before: timedelta, after: timedelta, taken: set):
    best = None
    for c in candidates:
        if c["id"] in taken:
            continue
        delta = c["booking_date"] - when
        if -before <= delta <= after and (best is None or abs(delta) < abs(best[0])):
            best = (delta, c)
    return best[1] if best else None


def lent(txns: list[dict], loans: list[dict]) -> dict[int, tuple]:
    """Money lent out and its repayments.
    txns: id, booking_date, amount_minor, description, counterparty.
    loans: txn_id (the payment out), repaid_by (a regex, any case, on the
    counterparty and description of the repayments), until (optional: the
    day of the last payment on the loan).
    Returns txn id -> (kind, partner id, rule, principal, interest).

    Repayments are the incoming payments matching the pattern, from the
    day of the loan on, oldest first. They pay off the principal first;
    the payment that clears it is split, and the part beyond the principal
    is interest. Without until, the loan ends there and later payments are
    ordinary income. With until, every matching payment up to that day
    belongs to the loan, and those after the principal is cleared are
    interest. The total interest is exact; within the period it shows
    when the principal is cleared, not as it accrues, since the bank text
    does not split a payment into principal and interest."""
    out: dict[int, tuple] = {}
    by_id = {t["id"]: t for t in txns}
    ordered = sorted(txns, key=lambda t: (t["booking_date"], t["id"]))
    known = [l for l in loans if l["txn_id"] in by_id]      # a manual mark wins
    for loan in sorted(known, key=lambda l: (by_id[l["txn_id"]]["booking_date"], l["txn_id"])):
        lent_out = by_id[loan["txn_id"]]
        if lent_out["amount_minor"] >= 0 or lent_out["id"] in out:
            continue
        try:
            pattern = re.compile(loan["repaid_by"], re.I)
        except re.error:
            log.warning("lending %s has a broken pattern, skipped", loan["txn_id"])
            continue
        out[lent_out["id"]] = ("lending", None, "lending:out", lent_out["amount_minor"], 0)
        outstanding = -lent_out["amount_minor"]
        until = loan.get("until")
        for t in ordered:
            if (outstanding <= 0 and until is None) or (until and t["booking_date"] > until):
                break
            if (t["id"] in out or t["amount_minor"] <= 0
                    or t["booking_date"] < lent_out["booking_date"]
                    or not pattern.search(categories.match_text(t.get("counterparty"),
                                                                t["description"]))):
                continue
            principal = min(t["amount_minor"], outstanding)
            outstanding -= principal
            out[t["id"]] = ("lending", lent_out["id"], "lending:repaid", principal,
                            t["amount_minor"] - principal)
    return out


def classify(txns: list[dict], accounts: dict[int, dict]) -> dict[int, tuple]:
    """txns: id, account_id, booking_date, amount_minor, description.
    accounts: id -> account_key, aspsp_name, cash_account_type.
    Returns txn id -> (kind, partner id or None, rule)."""
    out: dict[int, tuple] = {}
    taken: set[int] = set()
    ordered = sorted(txns, key=lambda t: (t["booking_date"], t["id"]))
    is_card = {i: a.get("cash_account_type") == "CARD" for i, a in accounts.items()}
    profile = {i: banks.for_bank(a.get("aspsp_name")) for i, a in accounts.items()}
    holders = {_name_key(a.get("name")) for a in accounts.values()
               if len(_name_key(a.get("name"))) >= 2}

    def bank(t) -> banks.BankProfile:
        return profile.get(t["account_id"], banks.NO_PROFILE)

    def own_move(t) -> bool:
        text, p = t["description"] or "", bank(t)
        if p.own_transfer and p.own_transfer.search(text):
            return True
        m = p.transfer_to_name.match(text) if p.transfer_to_name else None
        return bool(m) and _name_key(m["name"]) in holders

    # 1. Moves between own accounts: "Mellom Egne Konti", or a transfer to
    # an account in a household member's own name
    own = [t for t in ordered if own_move(t)]
    incoming = defaultdict(list)
    for t in own:
        if t["amount_minor"] > 0:
            incoming[t["amount_minor"]].append(t)
    for t in own:
        if t["amount_minor"] >= 0 or t["id"] in taken:
            continue
        partners = [c for c in incoming[-t["amount_minor"]] if c["account_id"] != t["account_id"]]
        p = _closest(partners, t["booking_date"], OWN_WINDOW, OWN_WINDOW, taken)
        if p:
            taken.update((t["id"], p["id"]))
            out[t["id"]] = ("own_transfer", p["id"], "own:paired")
            out[p["id"]] = ("own_transfer", t["id"], "own:paired")
    for t in own:
        if t["id"] not in out:
            # The other side is in an account the bank does not share: a
            # savings account the profile names (DNB's ASK), else what the
            # profile says (for DNB the credit-line mortgage).
            p = bank(t)
            text = t["description"] or ""
            saving = next((r for r, pattern in p.unpaired_saving if pattern.search(text)), None)
            rule = p.unpaired_own_transfer
            out[t["id"]] = (("saving", None, saving) if saving
                            else (rule.split(":")[0], None, rule) if rule
                            else ("own_transfer", None, "own:unpaired"))

    # 2. Card bills, paid from a bank account
    card_credits = defaultdict(list)
    for t in ordered:
        if is_card.get(t["account_id"]) and t["amount_minor"] > 0 and t["id"] not in out:
            card_credits[t["amount_minor"]].append(t)
    for t in ordered:
        if t["id"] in out or t["amount_minor"] >= 0 or is_card.get(t["account_id"]):
            continue
        for brand, pattern in bank(t).card_bills:
            if not pattern.search(t["description"] or ""):
                continue
            is_brand = CARDS.get(brand, lambda a: False)
            partners = [c for c in card_credits[-t["amount_minor"]]
                        if is_brand(accounts[c["account_id"]])]
            p = _closest(partners, t["booking_date"], CARD_BEFORE, CARD_AFTER, taken)
            if p:
                taken.update((t["id"], p["id"]))
                out[p["id"]] = ("card_payment", t["id"], f"card:{brand}")
            out[t["id"]] = ("card_payment", p["id"] if p else None, f"card:{brand}")
            break
    for t in ordered:
        if (t["id"] not in out and is_card.get(t["account_id"]) and t["amount_minor"] > 0
                and CARD_CREDIT_TEXT.search(t["description"] or "")):
            out[t["id"]] = ("card_payment", None, "card:credit_unmatched")

    # 3. Reimbursed expenses ("utlegg"), paired with the expense if found
    expenses = defaultdict(list)
    for t in ordered:
        if t["amount_minor"] < 0 and t["id"] not in out:
            expenses[-t["amount_minor"]].append(t)
    for t in ordered:
        if t["id"] in out or t["amount_minor"] <= 0 or not REIMBURSEMENT.search(t["description"] or ""):
            continue
        p = _closest(expenses[t["amount_minor"]], t["booking_date"], REIMBURSEMENT_WINDOW,
                     timedelta(days=7), taken)
        if p:
            taken.add(p["id"])
        out[t["id"]] = ("reimbursement", p["id"] if p else None, "reimbursement:utlegg")
    for t in ordered:
        if t["id"] in out or t["amount_minor"] <= 0:
            continue
        hit = next((rule for rule, pattern in COST_REDUCTION
                    if pattern.search(f"{t.get('counterparty') or ''} {t['description'] or ''}")), None)
        if hit:
            out[t["id"]] = ("reimbursement", None, hit)

    # 4. Saving and loans: one side only
    for t in ordered:
        if t["id"] in out:
            continue
        text, p = t["description"] or "", bank(t)
        # Saving is money put away. Money coming back from the same place
        # is a dividend or a sale: income, not negative saving.
        saving = (p.saving + SAVING if t["amount_minor"] < 0
                  else () if p.dividend and p.dividend.search(text) else p.saving_out)
        for kind, rules in (("saving", saving), ("loan", p.loans + LOANS)):
            hit = next((rule for rule, pattern in rules if pattern.search(text)), None)
            if hit:
                out[t["id"]] = (kind, None, hit)
                break
    return out


async def run(conn) -> dict:
    """Recompute every automatic classification. Returns counts per rule."""
    accounts = {r["id"]: dict(r) for r in await conn.fetch(
        "SELECT id, account_key, aspsp_name, cash_account_type, name FROM account")}
    txns = [dict(r) for r in await conn.fetch(
        """SELECT t.id, t.account_id, t.booking_date, t.amount_minor, t.description, t.counterparty
           FROM txn t
           WHERE NOT EXISTS (SELECT 1 FROM txn_class c WHERE c.txn_id = t.id AND c.manual)""")]
    loans = [dict(r) for r in await conn.fetch("SELECT txn_id, repaid_by, until FROM lending")]
    lending = lent(txns, loans)
    result = classify([t for t in txns if t["id"] not in lending], accounts)
    by_id = {t["id"]: t for t in txns}
    rows = [(tid, kind, partner, rule, principal, interest, 0)
            for tid, (kind, partner, rule, principal, interest) in lending.items()]
    for tid, (kind, partner, rule) in result.items():
        t = by_id[tid]
        split = (banks.for_bank((accounts.get(t["account_id"]) or {}).get("aspsp_name"))
                 .loan_split(t["description"], t["amount_minor"])
                 if kind == "loan" else (None, None, None))
        rows.append((tid, kind, partner, rule, *split))
    async with conn.transaction():
        await conn.execute("DELETE FROM txn_class WHERE NOT manual")
        await conn.executemany(
            """INSERT INTO txn_class (txn_id, kind, partner_txn_id, rule,
                                      principal_minor, interest_minor, fees_minor)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""", rows)
    counts = defaultdict(int)
    for _, _, rule, *_ in lending.values():
        counts[rule] += 1
    for kind, partner, rule in result.values():
        counts[rule] += 1
        if kind in ("own_transfer", "card_payment") and partner is None:
            counts["no partner"] += 1
    log.info("pairing: %s", dict(sorted(counts.items())))
    await categories.run(conn)          # spending depends on the classification
    await tax.refresh(conn)             # so does the interest in the tax estimate
    return dict(counts)
