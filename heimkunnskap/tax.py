"""The income tax estimate, per person, for one income year.

compute() is pure: one person's inputs and the year's rules in, the tax
and its parts out. gather() reads the inputs from the ledger:

  salary      from the latest pay slip per employer: year to date, plus the
              rest of the year at this month's regular salary and benefits.
              Withholding the same way, with half in December (the
              "half tax" month for table withholding), unless the slip
              shows half tax taken already (some employers take it in
              November: half the month before's withholding).
  business    deposits into the business-income account, less their VAT:
              none before the VAT registration date, then 20% of each
              deposit (25% on top of the price) unless business_vat says
              otherwise. Refunds from Skatteetaten are not income. Then
              less the tagged business expenses excl. VAT (business.py).
  interest    loan interest paid (the interest part of each instalment),
              projected for the months left from the last three months. Each
              spouse deducts the interest on their own loans, so it is split:
              loan_interest_share (0-1, default 1) of it to loan_interest_person
              (default the business owner), the rest to the other person.
  dividends   "Utbytte" and payouts from employee share companies, to the
              person whose account received them, times the dividend
              factor, with no shielding deduction (slightly high).

Not covered: wealth tax, student loan interest (Lånekassen's statement in
January has the figure), credit interest (paid in December, a few kroner),
gains on funds and shares sold.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from .payslips import name_key

log = logging.getLogger("heimkunnskap.tax")

DIVIDEND = re.compile(r"\bUtbytte\b|\bEmployee Invest\b", re.I)
TAX_AUTHORITY = re.compile(r"Skatteetaten|Skatteinnkreving|Innkrevingsmynd", re.I)


@dataclass
class Inputs:
    person: str
    salary: int = 0                 # øre, the year's taxable salary incl. benefits
    pension: int = 0                # own pension contributions, deductible
    business_profit: int = 0
    dividends: int = 0
    interest_paid: int = 0
    other_deduction: int = 0        # gifts to voluntary organisations, management fees
    withheld: int = 0               # tax withheld for the year, projected
    prepaid: int = 0                # advance tax paid directly
    notes: list[str] = field(default_factory=list)


def bracket_tax(personal_income: float, brackets: list) -> float:
    tax = 0.0
    for i, (threshold, rate) in enumerate(brackets):
        upper = brackets[i + 1][0] if i + 1 < len(brackets) else None
        if personal_income > threshold:
            top = personal_income if upper is None else min(personal_income, upper)
            tax += (top - threshold) * rate
    return tax


def compute(inp: Inputs, rules: dict) -> dict:
    """All amounts in kroner (floats) in the result."""
    kr = lambda m: float(m) / 100                              # noqa: E731  (m may be a Decimal)
    salary, profit = kr(inp.salary), kr(inp.business_profit)
    minimum = min(salary * rules["minimum_deduction_rate"], rules["minimum_deduction_max"])
    dividends = kr(inp.dividends) * rules["dividend_factor"]
    general_income = (salary + profit + dividends - minimum - kr(inp.pension)
                      - kr(inp.interest_paid) - kr(inp.other_deduction))
    taxable = max(0.0, general_income - rules["personal_allowance"])
    general_tax = taxable * rules["general_rate"]
    personal_income = salary + max(profit, 0.0)
    brackets = bracket_tax(personal_income, rules["brackets"])
    social = salary * rules["social_security_salary"] + max(profit, 0.0) * rules["social_security_business"]
    floor = rules.get("social_security_floor", 0)
    if personal_income <= floor:
        social = 0.0
    else:
        social = min(social, (personal_income - floor) * 0.25)
    total = general_tax + brackets + social
    paid = kr(inp.withheld + inp.prepaid)
    top = next((r for t, r in reversed(rules["brackets"]) if personal_income > t), 0.0)
    return {
        "person": inp.person,
        "salary": salary, "business_profit": profit, "dividends": kr(inp.dividends),
        "minimum_deduction": minimum, "pension_deduction": kr(inp.pension),
        "interest_deduction": kr(inp.interest_paid), "other_deduction": kr(inp.other_deduction),
        "general_income": general_income, "personal_allowance": rules["personal_allowance"],
        "general_tax": general_tax, "personal_income": personal_income,
        "bracket_tax": brackets, "social_security": social, "total_tax": total,
        "withheld": kr(inp.withheld), "prepaid": kr(inp.prepaid),
        "balance": paid - total,                 # positive: refund; negative: tax to pay
        "marginal_rate_salary": rules["general_rate"] + rules["social_security_salary"] + top,
        "marginal_rate_business": (rules["general_rate"] + rules["social_security_business"] + top
                                   if profit > 0 else None),
        "notes": list(inp.notes),
    }


# -- reading the ledger

def half_tax_taken(slip: dict, previous: dict | None) -> bool:
    """Whether this slip's month was the half-tax month: its withholding is
    half the month before's at about the same pay. Some employers take
    half tax in November, so December is then full."""
    if not previous or previous["withholding_minor"] <= 0 or slip["period"].month == 12:
        return False
    pay, before = (slip["salary_minor"] + slip["benefits_minor"],
                   previous["salary_minor"] + previous["benefits_minor"])
    return (abs(2 * slip["withholding_minor"] - previous["withholding_minor"])
            <= 0.03 * previous["withholding_minor"]
            and abs(pay - before) <= 0.1 * max(before, 1))


def project_slip(slip: dict, year: int, previous: dict | None = None
                 ) -> tuple[int, int, int, list[str]]:
    """(salary, pension, withheld) for the whole year from the latest slip
    of one employer, and notes. previous: the employer's slip before it,
    to see whether half tax has been taken already."""
    left = 12 - slip["period"].month if slip["period"].year == year else 0
    salary = slip["gross_ytd_minor"] + left * (slip["salary_minor"] + slip["benefits_minor"])
    pension = slip["pension_ytd_minor"] + left * slip["pension_minor"]
    half_taken = left > 0 and half_tax_taken(slip, previous)
    if half_taken:
        # Half tax was this month; the rest of the year is full
        withheld = slip["withheld_ytd_minor"] + previous["withholding_minor"] * left
    else:
        # The half-tax month (December for table withholding) is still to come
        withheld = slip["withheld_ytd_minor"] + slip["withholding_minor"] * left
        if left:
            withheld -= slip["withholding_minor"] // 2
    note = (f"{slip['employer']}: pay slip for {slip['period']:%m/%Y}"
            + (f", {left} more month{'s' if left > 1 else ''} at "
               f"{(slip['salary_minor'] + slip['benefits_minor']) / 100:,.0f} kr".replace(",", " ")
               if left else ", the year's last")
            + (f"; half tax taken in {slip['period']:%m/%Y}, so withholding after it at the "
               f"full {previous['withholding_minor'] / 100:,.0f} kr".replace(",", " ")
               if half_taken else ""))
    return salary, pension, withheld, [note]


def monthly_interest_projection(by_month: dict[int, int], today: date, year: int) -> tuple[int, str]:
    """Interest paid for the year: actual months, and for this month and the
    rest the average of the last three complete months (if higher than what
    is booked so far)."""
    if year < today.year:
        return sum(by_month.values()), "interest paid, whole year"
    complete = [m for m in range(1, today.month) if m in by_month]
    recent = [by_month[m] for m in complete[-3:]]
    avg = round(sum(recent) / len(recent)) if recent else 0
    total = sum(by_month.get(m, 0) for m in range(1, today.month))
    for m in range(today.month, 13):
        total += max(by_month.get(m, 0), avg)
    return total, (f"loan interest {sum(by_month.values()) / 100:,.0f} kr paid so far, "
                   f"the rest of the year at {avg / 100:,.0f} kr a month").replace(",", " ")


async def gather(conn, year: int, today: date | None = None) -> list[Inputs]:
    today = today or date.today()
    persons = {r["id"]: dict(r) for r in await conn.fetch("SELECT id, name, name_key FROM person")}
    if not persons:
        return []
    inputs = {pid: Inputs(person=p["name"]) for pid, p in persons.items()}
    by_key = {p["name_key"]: pid for pid, p in persons.items()}

    # Salary: the latest slip of the year per person and employer
    slips = await conn.fetch(
        """SELECT DISTINCT ON (person_id, employer) * FROM payslip
           WHERE extract(year FROM period) = $1
           ORDER BY person_id, employer, period DESC""", year)
    for s in slips:
        previous = await conn.fetchrow(
            """SELECT * FROM payslip WHERE person_id = $1 AND employer = $2 AND period < $3
                 AND extract(year FROM period) = $4 ORDER BY period DESC LIMIT 1""",
            s["person_id"], s["employer"], s["period"], year)
        salary, pension, withheld, notes = project_slip(dict(s), year,
                                                        dict(previous) if previous else None)
        i = inputs[s["person_id"]]
        i.salary += salary
        i.pension += pension
        i.withheld += withheld
        i.notes += notes
    for pid, i in inputs.items():
        if not any(s["person_id"] == pid for s in slips):
            i.notes.append(f"no pay slip for {year}: salary and withholding not counted")

    # Who each account belongs to: the holder's name, else whose pay lands there
    accounts = await conn.fetch("SELECT id, name, is_business_income FROM account")
    owner = {a["id"]: by_key.get(name_key(a["name"])) for a in accounts}
    paid_into = await conn.fetch(
        """SELECT DISTINCT ON (p.person_id) p.person_id, t.account_id
           FROM payslip p JOIN txn t ON t.amount_minor = p.net_paid_minor
            AND t.booking_date BETWEEN p.pay_date - 3 AND p.pay_date + 5
           ORDER BY p.person_id, p.period DESC""")
    for r in paid_into:
        if owner.get(r["account_id"]) is None:
            owner[r["account_id"]] = r["person_id"]

    # Business: the owner of the business-income account
    business_accounts = [a["id"] for a in accounts if a["is_business_income"]]
    business_owner = next((owner.get(a) for a in business_accounts if owner.get(a)), None)
    if business_accounts:
        registered = await conn.fetchval("SELECT value FROM app_setting WHERE key = 'vat_registered_from'")
        reg_date = date.fromisoformat(str(registered)[:10]) if registered else None
        rows = await conn.fetch(
            """SELECT t.id, t.booking_date, t.amount_minor, t.description, v.vat_minor
               FROM txn t LEFT JOIN txn_class c ON c.txn_id = t.id
               LEFT JOIN business_vat v ON v.txn_id = t.id
               WHERE t.account_id IN (SELECT id FROM account WHERE is_business_income)
                 AND t.amount_minor > 0 AND c.kind IS NULL
                 AND extract(year FROM t.booking_date) = $1""", year)
        income = vat = 0
        for r in rows:
            if TAX_AUTHORITY.search(r["description"] or ""):
                continue                                   # a VAT refund, not income
            v = (r["vat_minor"] if r["vat_minor"] is not None
                 else r["amount_minor"] // 5 if reg_date and r["booking_date"] >= reg_date else 0)
            income += r["amount_minor"]
            vat += v
        cost = await conn.fetchrow(
            """SELECT count(*) AS n, coalesce(sum(cost_minor - input_vat_minor), 0) AS excl_vat
               FROM report.business_expenses WHERE extract(year FROM booking_date) = $1""", year)
        expenses = int(cost["excl_vat"])
        if business_owner and (income or expenses):
            i = inputs[business_owner]
            i.business_profit += income - vat - expenses
            i.notes.append(f"business: {income / 100:,.0f} kr received, {vat / 100:,.0f} kr of it VAT; "
                           f"{cost['n']} tagged expenses, {expenses / 100:,.0f} kr excl. VAT"
                           .replace(",", " "))
            if not reg_date:
                i.notes.append("no VAT registration date set: all business income counted without VAT")

    # Loan interest, to one person
    chosen = await conn.fetchval("SELECT value FROM app_setting WHERE key = 'loan_interest_person'")
    interest_person = by_key.get(name_key(chosen)) if chosen else None
    interest_person = interest_person or business_owner or min(persons)
    rows = await conn.fetch(
        """SELECT extract(month FROM t.booking_date)::int AS m, -sum(c.interest_minor) AS interest
           FROM txn_class c JOIN txn t ON t.id = c.txn_id
           WHERE c.kind = 'loan' AND c.interest_minor IS NOT NULL
             AND extract(year FROM t.booking_date) = $1
           GROUP BY 1""", year)
    # sum() of bigint comes back as Decimal from the database driver: whole øre, as int
    interest, note = monthly_interest_projection({r["m"]: int(r["interest"]) for r in rows},
                                                 today, year)
    share_text = await conn.fetchval("SELECT value FROM app_setting WHERE key = 'loan_interest_share'")
    try:
        share = min(1.0, max(0.0, float(share_text))) if share_text not in (None, "") else 1.0
    except ValueError:
        share = 1.0
    others = [pid for pid in inputs if pid != interest_person]
    if len(others) != 1:
        share = 1.0                                         # a split needs exactly two people
    mine = round(interest * share)
    inputs[interest_person].interest_paid += mine
    inputs[interest_person].notes.append(
        note + (f" ({share:.0%} deducted here, the rest by the spouse)" if share < 1
                else " (all deducted here; set loan_interest_share to split it)"))
    if share < 1:
        inputs[others[0]].interest_paid += interest - mine
        inputs[others[0]].notes.append(f"loan interest: {1 - share:.0%} of the household's, "
                                       "as set in loan_interest_share")
    if await conn.fetchval(
            """SELECT count(*) FROM txn_class c JOIN txn t ON t.id = c.txn_id
               WHERE c.rule = 'loan:student' AND extract(year FROM t.booking_date) = $1""", year):
        inputs[interest_person].notes.append(
            "student loan payments found: their interest is not split out, so not deducted "
            "(Lånekassen's statement in January has it)")

    # Dividends, to whoever's account received them
    rows = await conn.fetch(
        """SELECT t.account_id, t.amount_minor, t.description FROM txn t
           LEFT JOIN txn_class c ON c.txn_id = t.id
           WHERE t.amount_minor > 0 AND c.kind IS NULL AND extract(year FROM t.booking_date) = $1""", year)
    for r in rows:
        if DIVIDEND.search(r["description"] or ""):
            pid = owner.get(r["account_id"]) or interest_person
            inputs[pid].dividends += r["amount_minor"]
    for i in inputs.values():
        if i.dividends:
            i.notes.append(f"dividends {i.dividends / 100:,.0f} kr, counted without the shielding "
                           "deduction".replace(",", " "))

    # Advance tax paid directly (sole proprietors)
    rows = await conn.fetch(
        """SELECT t.account_id, -t.amount_minor AS paid FROM txn t
           WHERE t.description ~* 'forskuddsskatt' AND t.amount_minor < 0
             AND extract(year FROM t.booking_date) = $1""", year)
    for r in rows:
        pid = owner.get(r["account_id"]) or business_owner or interest_person
        inputs[pid].prepaid += r["paid"]
    return list(inputs.values())


async def rules_for(conn, year: int) -> dict | None:
    params = await conn.fetchval("SELECT params FROM tax_rule WHERE year = $1", year)
    if params is None:
        return None
    return json.loads(params) if isinstance(params, str) else dict(params)


async def estimate(conn, year: int, today: date | None = None) -> dict:
    rules = await rules_for(conn, year)
    if rules is None:
        return {"year": year, "error": f"no tax rules for {year}"}
    if not await conn.fetchval("SELECT count(*) FROM payslip WHERE extract(year FROM period) = $1",
                               year):
        return {"year": year, "as_of": (today or date.today()).isoformat(), "people": [],
                "household": {}}                  # nothing to estimate from
    people = [compute(i, rules) for i in await gather(conn, year, today)]
    total = {k: sum(p[k] for p in people) for k in ("total_tax", "withheld", "prepaid", "balance")}
    return {"year": year, "as_of": (today or date.today()).isoformat(), "people": people,
            "household": total}


async def refresh(conn, today: date | None = None) -> None:
    """Store this year's estimate (and last year's until its assessment
    is out in the autumn) for the report views. Never fatal."""
    today = today or date.today()
    try:
        if not await conn.fetchval("SELECT count(*) FROM person"):
            return
        for year in (today.year - 1, today.year):
            result = await estimate(conn, year, today)
            if "error" in result or not result["people"]:
                continue
            await conn.execute(
                """INSERT INTO tax_estimate (year, as_of, result) VALUES ($1, $2, $3::jsonb)
                   ON CONFLICT (year) DO UPDATE SET as_of = excluded.as_of,
                       result = excluded.result, updated_at = now()""",
                year, today, json.dumps(result))
    except Exception:                               # the estimate must not stop a sync
        log.exception("tax estimate failed")
