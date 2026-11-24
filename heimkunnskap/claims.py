"""Expense claims (reiseregning) from the employer's travel system, as PDFs:
what was claimed, how much of it is taxable, and which card payments and
which salary deposit it accounts for.

The layout (docs/formats/unit4-expense-claim.md) is a Unit4 travel system claim: a
header (claim number, person, trip dates, purpose), allowances (per diem
less meals provided), costs (outlays such as hotel and taxi, and allowances
such as mileage), the total, and the tax-free and taxable parts. Some PDFs
have a text layer, read with `pdftotext -raw`; others are scans, read with
`tesseract`. Both give one line per row of the claim. A claim is accepted
only if its lines add up to its own totals, which also catches misreads.

How a claim changes the ledger (match and parts, run after every pairing):

  outlays     each is matched to the card payment it was, by amount and
              date (a hotel bill may be larger than the claim: the extras
              not claimed stay as the household's own spending)
  payout      the claim's total less tax withheld on the taxable part is
              one salary deposit; several claims may share one
  parts       that deposit is split: the outlays it pays back reduce
              spending in the category of the card payment (Travel, Car
              and transport, ...), in the deposit's month like any other
              reimbursement; the rest (allowances less tax) is income

The person's national identity number, address and employee number are
never copied out of the PDF, nor are the route lines of a mileage claim.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("heimkunnskap.claims")

NUM = r"-?\d+,\d+"
DATE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})\b\s*(.*)$")
STAMP = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s*-\s*\d{1,2}[:.]\d{2}")
FOUR = re.compile(rf"^(.*?)\s*({NUM})\s+({NUM})\s+({NUM})\s+({NUM})$")
CURRENCY = re.compile(rf"(?:^|\s)([A-Z]{{3}})\s+(\d+,\d+)\s+({NUM})$")

# Kinds of claim line, by how the type text starts (spaces removed, lower
# case; no "ø" in the keys, which OCR reads as "g" or "@").
OUTLAYS = [("hotel", ("hotell", "overnatting", "losji")), ("flight", ("fly",)),
           ("train", ("tog",)), ("taxi", ("taxi",)),
           ("transit", ("buss", "trikk", "t-bane", "kollektiv", "ferje", "hurtigb")),
           ("parking", ("p-avgift", "parkering")), ("toll", ("bompenger", "bom")),
           ("car_hire", ("leiebil",)), ("fuel", ("drivstoff",)),
           ("subscription", ("refusjonabonnement", "abonnement", "medlemsavgift", "kontingent"))]
ALLOWANCES = [("per_diem", ("diett", "kostgodtgj")), ("deduction", ("fratrekk",)),
              ("mileage", ("bilgodtgj", "kjoregodtgj", "kj@regodtgj", "kjgregodtgj",
                           "passasjertillegg", "tilleggforpassasjer")),
              ("allowance", ("kompensasjonstillegg", "nattillegg", "ulegitimert", "tillegg"))]
OUTLAY_KINDS = {k for k, _ in OUTLAYS} | {"other"}

# What the card payment's text should say, per outlay kind; "other" needs
# the exact amount instead
HINTS = {
    "hotel": r"hotel|hotell|hôtel|inn\b|lodge|resort|booking\.com|airbnb|expedia|hotels\.com",
    "flight": r"\bsas\b|scandinavian|norwegian|wider|klm|lufthansa|british a|air ?france|"
              r"finnair|ryanair|airline|airways|flysas|\bfly",
    "train": r"flytog|\bvy\b|vy app|\bnsb\b|\bsj\b|ratp|sncf|\btog\b|bahn|trenitalia|rail",
    "taxi": r"taxi|taxus|uber|\bcab\b|bolt|cabonline|norgestaxi|oslotaxi",
    "transit": r"ruter|skyss|atb|kolumbus|ratp|kollektiv|ferje|fjord1|norled",
    "parking": r"easypark|parker|parking|apcoa|onepark|aimo|europark|\bp-",
    "toll": r"autosync|bompenger|fjellinjen|ferde|vegamot|autopass|skyttel",
    "car_hire": r"hertz|avis|europcar|sixt|budget|enterprise|rent",
    "fuel": r"circle k|\besso\b|\bshell\b|uno-x|\byx\b|best\b",
}
# Kinds matched by the exact amount only (and the hint, if any)
EXACT = {"other", "subscription"}
# Days before and after the claimed date a card payment may be booked:
# flights are paid long before, card bookings lag a few days
WINDOWS = {"flight": (180, 10), "hotel": (120, 10), "subscription": (60, 10)}
WINDOW = (14, 10)
# Unmatched outlays count against these categories
DEFAULT_CATEGORY = {"toll": "Car and transport", "parking": "Car and transport",
                    "fuel": "Car and transport", "car_hire": "Travel", "hotel": "Travel",
                    "flight": "Travel", "train": "Travel", "taxi": "Travel", "transit": "Travel",
                    "subscription": "Subscriptions and media"}


class ClaimError(ValueError):
    """Not an expense claim this module can read, or one that does not add up."""


@dataclass
class ClaimLine:
    section: str                 # allowance or cost
    kind: str                    # hotel, taxi, ..., per_diem, mileage, other
    day: date
    text: str                    # the cost type as printed
    note: str
    amount: int                  # øre, NOK
    currency: str | None = None
    currency_amount: int | None = None     # øre of that currency

    @property
    def outlay(self) -> bool:
        return self.section == "cost" and self.kind in OUTLAY_KINDS


@dataclass
class Claim:
    claim_no: str
    title: str                   # e.g. Utenlandsreise - Statens regulativ
    person_name: str
    purpose: str
    trip_from: date
    trip_to: date
    total: int
    tax_free: int
    taxable: int
    lines: list[ClaimLine] = field(default_factory=list)
    ocr: bool = False

    @property
    def outlays(self) -> int:
        return sum(l.amount for l in self.lines if l.outlay)

    def summary(self) -> str:
        return (f"claim {self.claim_no}, {self.purpose or self.title}, "
                f"{self.trip_from:%d.%m}-{self.trip_to:%d.%m.%Y}: {self.total / 100:,.2f}, "
                f"taxable {self.taxable / 100:,.2f}").replace(",", " ")


def minor(text: str) -> int:
    return round(float(text.replace(" ", "").replace(",", ".")) * 100)


def _key(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def kind_of(type_text: str, section: str) -> str | None:
    """The kind of a claim line from its type text, None if the text is not
    a cost type (a note, a route)."""
    k = _key(type_text)
    for kind, starts in ALLOWANCES + (OUTLAYS if section == "cost" else []):
        if k.startswith(starts):
            return kind
    return None


def _date(d: str, m: str, y: str) -> date:
    return date(int(y), int(m), int(d))


def detect(text: str) -> bool:
    """A travel claim has a Totalsum line; an outlay claim (Utlegg) only the
    summary at the end. Both have the claim ID and the tax-free part."""
    return (re.search(r"\bID\s*:\s*\d+", text) is not None and "Skattefri del" in text
            and ("Totalsum" in text or re.search(r"^Sum \(NOK\)", text, re.M) is not None))


def parse_text(text: str, *, ocr: bool = False) -> Claim:
    """A claim from its text, one row of the claim per line."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]
    if not detect("\n".join(lines)):
        raise ClaimError("not an expense claim this app can read")
    head = next(l for l in lines if re.search(r"\bID\s*:\s*\d+", l))
    claim_no = re.search(r"\bID\s*:\s*(\d+)", head).group(1)
    title = re.sub(r"\s*\bID\s*:\s*\d+.*$", "", head).strip()

    name = ""
    for i, l in enumerate(lines):
        if l.startswith("Ansatt") and i + 1 < len(lines):
            m = re.match(r"(.+?)\s*\(\d+\)", lines[i + 1])
            name = (m.group(1) if m else lines[i + 1]).strip()
            break
    purpose = next((re.sub(r"^Hensikt med reisen/utlegget\s*", "", l) for l in lines
                    if l.startswith("Hensikt med")), "").strip()
    stamps = [_date(*m.groups()) for l in lines for m in STAMP.finditer(l)]

    totals: dict[str, int] = {}
    items: list[ClaimLine] = []
    section = None
    day = None
    current_type = ""
    pending: list[str] = []          # text since the last amounts: type wrap, note
    currency = None
    dated = False                    # a date line since the last row of amounts

    for l in lines:
        if l.startswith("DIETTER OG FRATREKK"):
            section, pending = "allowance", []
            continue
        if l.startswith("KOSTNADER"):
            section, pending = "cost", []
            continue
        if l.startswith("INFORMASJON"):
            section = None
            continue
        m = re.match(rf"^(Totalsum|Sum Kostnader|Sum Dietter|Sum \(NOK\))[^\d-]*({NUM})", l)
        if m:
            totals[m.group(1)] = minor(m.group(2))
            section = None if m.group(1) == "Totalsum" else section
            continue
        m = re.match(rf"^Skatte(fri|belagt) del\s+({NUM})$", l)
        if m:
            totals[m.group(1)] = minor(m.group(2))
            continue
        if section is None or l.startswith(("Dato ", "Merknad ")):
            continue
        dm = DATE.match(l)
        if dm and not STAMP.match(l):
            day = _date(*dm.groups()[:3])
            rest = dm.group(4)
            cm = CURRENCY.search(rest)
            currency = (cm.group(1), minor(cm.group(3))) if cm else None
            current_type = rest[:cm.start()].strip() if cm else rest
            pending, dated = [], True
            continue
        cm = CURRENCY.fullmatch(l)
        if cm:
            currency = (cm.group(1), minor(cm.group(3)))
            continue
        fm = FOUR.match(l)
        if fm and day is not None:
            note_part = fm.group(1).strip()
            amount = minor(fm.group(4))
            type_text = current_type
            # an undated row below another (a toll passing under a mileage
            # claim) names its own type on the line above; below a date line
            # the text is a wrapped type or a note ("Fratrekk på 5 Euro ...")
            if not dated:
                for p in reversed(pending):
                    if kind_of(p, section):
                        type_text = p
                        current_type = p
                        break
            notes = [p for p in pending if p != type_text and "->" not in p]
            note = " ".join(notes + ([note_part] if note_part not in ("", "_") else [])).strip()
            kind = kind_of(type_text, section) or ("other" if section == "cost" else "allowance")
            items.append(ClaimLine(section, kind, day, type_text, note, amount,
                                   currency[0] if currency else None,
                                   currency[1] if currency else None))
            pending, currency, dated = [], None, False
            continue
        pending.append(l)

    total = totals.get("Totalsum", totals.get("Sum (NOK)"))
    if total is None:
        raise ClaimError("expense claim: no total found")
    tax_free, taxable = totals.get("fri", 0), totals.get("belagt", 0)
    problems = []
    if sum(i.amount for i in items) != total:
        problems.append(f"the lines add up to {sum(i.amount for i in items) / 100:.2f}, "
                        f"the total is {total / 100:.2f}")
    if not items:
        problems.append("no lines found")
    for key, sec in (("Sum Kostnader", "cost"), ("Sum Dietter", "allowance")):
        if key in totals and sum(i.amount for i in items if i.section == sec) != totals[key]:
            problems.append(f"{key} does not match its lines")
    if tax_free + taxable != total:
        problems.append("the tax-free and taxable parts do not add up to the total")
    if problems:
        raise ClaimError("expense claim " + claim_no + " misread"
                         + (" (scanned, read by OCR)" if ocr else "") + ": " + "; ".join(problems))
    days = [i.day for i in items]
    return Claim(claim_no=claim_no, title=title, person_name=name, purpose=purpose,
                 trip_from=stamps[0] if stamps else min(days),
                 trip_to=stamps[1] if len(stamps) > 1 else max(days),
                 total=total, tax_free=tax_free, taxable=taxable, lines=items, ocr=ocr)


def _run(cmd: list[str], **kw) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True,
                              timeout=180, **kw).stdout
    except FileNotFoundError:
        raise ClaimError(f"{cmd[0]} is not installed (the Docker image has it)") from None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        said = (getattr(exc, "stderr", None) or "").strip().splitlines()
        raise ClaimError(f"{cmd[0]} could not read the PDF"
                         + (f": {said[-1][:120]}" if said else f": {type(exc).__name__}")) from None


def pdf_text(content: bytes) -> tuple[str, bool]:
    """The claim's text: the PDF's own text layer if it has one, else OCR
    of each page at 300 dpi. Returns (text, read by OCR)."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "claim.pdf"
        pdf.write_bytes(content)
        text = _run(["pdftotext", "-raw", str(pdf), "-"])
        if detect(text):
            return text, False
        _run(["pdftoppm", "-r", "300", "-png", str(pdf), str(Path(tmp) / "page")])
        pages = sorted(Path(tmp).glob("page*.png"))
        if not pages:
            raise ClaimError("the PDF has no pages")
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        return "\n".join(_run(["tesseract", str(p), "-", "--psm", "6"], env=env)
                         for p in pages), True


def looks_like_claim(content: bytes) -> bool:
    try:
        text, _ = pdf_text(content)
    except ClaimError:
        return False
    return detect(text)


def parse(content: bytes) -> Claim:
    text, ocr = pdf_text(content)
    return parse_text(text, ocr=ocr)


# -- Matching claims to the ledger

@dataclass
class Txn:
    id: int
    booking_date: date
    amount: int                  # øre, negative for money out
    text: str


def acceptable(kind: str, claimed: int, paid: int, foreign: bool) -> bool:
    """Whether a card payment of paid øre can be the outlay claimed. Hotel
    bills may be larger (extras not claimed); foreign amounts differ by the
    exchange rate the employer used."""
    if paid == claimed:
        return True
    if kind == "hotel" and claimed <= paid <= claimed * 1.25:
        return True
    slack = 0.04 if foreign else 0.005
    return abs(paid - claimed) <= max(100, claimed * slack) if kind not in EXACT else False


def candidates(line: ClaimLine, txns: list[Txn]) -> list[Txn]:
    before, after = WINDOWS.get(line.kind, WINDOW)
    hint = HINTS.get(line.kind)
    out = []
    for t in txns:
        if t.amount >= 0:
            continue
        if not (line.day - timedelta(days=before) <= t.booking_date <= line.day + timedelta(days=after)):
            continue
        if hint and not re.search(hint, t.text, re.I):
            continue
        if acceptable(line.kind, line.amount, -t.amount, line.currency is not None):
            out.append(t)
    return sorted(out, key=lambda t: (abs(-t.amount - line.amount),
                                      abs((t.booking_date - line.day).days), t.id))


def match_lines(lines: list[tuple[int, ClaimLine]], txns: list[Txn],
                taken: set[int]) -> dict[int, tuple[int | None, bool]]:
    """Claim line id -> (card payment id or None, ambiguous). Each payment
    is used once; exact amounts are matched first, so near misses do not
    take an exact payment from another line."""
    out = {}
    order = sorted(lines, key=lambda il: min((abs(-t.amount - il[1].amount)
                                               for t in candidates(il[1], txns)), default=10**12))
    for lid, line in order:
        found = [t for t in candidates(line, txns) if t.id not in taken]
        if not found:
            out[lid] = (None, False)
            continue
        best = found[0]
        taken.add(best.id)
        # a guess if another payment fits almost as well; two payments that
        # differ only in id (two tickets bought together) are interchangeable
        near = abs(-best.amount - line.amount) + max(200, line.amount // 200)
        days = abs((best.booking_date - line.day).days) + 2
        others = [t for t in found[1:] if abs(-t.amount - line.amount) <= near
                  and abs((t.booking_date - line.day).days) <= days
                  and (t.amount, t.booking_date) != (best.amount, best.booking_date)]
        out[lid] = (best.id, bool(others))
    return out


def expected_payouts(total: int, taxable: int, percents: list[float]) -> list[int]:
    """The deposit a claim becomes: the total less tax withheld at the
    person's percentage on the taxable part, in whole kroner."""
    amounts = [total]
    for p in percents:
        amounts.append(total - int(taxable * p / 100 // 100) * 100)
    return sorted(set(amounts), key=lambda a: amounts.index(a))


async def match_claims(conn) -> dict:
    """Match every claim's outlays to card payments, and each claim to its
    payout, then rebuild the deposit parts. Manual choices are kept."""
    claims = await conn.fetch(
        """SELECT c.id, c.person_id, c.total_minor, c.taxable_minor, c.trip_to,
                  c.payout_txn_id, c.payout_manual
           FROM expense_claim c ORDER BY c.trip_from, c.id""")
    if not claims:
        await conn.execute("DELETE FROM txn_part WHERE source = 'claim'")
        return {}
    rows = await conn.fetch(
        """SELECT t.id, t.booking_date, t.amount_minor,
                  coalesce(t.counterparty, '') || ' ' || coalesce(t.description, '') AS text
           FROM txn t LEFT JOIN txn_class c ON c.txn_id = t.id
           WHERE c.kind IS NULL""")
    txns = [Txn(r["id"], r["booking_date"], r["amount_minor"], r["text"]) for r in rows]
    by_id = {t.id: t for t in txns}
    lines = await conn.fetch(
        """SELECT id, claim_id, section, kind, day, amount_minor, currency, txn_id, match
           FROM expense_claim_line ORDER BY claim_id, line_no""")
    taken = {l["txn_id"] for l in lines if l["match"] in ("manual", "none") and l["txn_id"]}
    todo = []
    for l in lines:
        if (l["match"] in ("manual", "none") or l["section"] != "cost"
                or l["kind"] not in OUTLAY_KINDS or l["amount_minor"] <= 0):
            continue
        todo.append((l["id"], ClaimLine(l["section"], l["kind"], l["day"], "", "", l["amount_minor"],
                                        l["currency"])))
    found = match_lines(todo, txns, taken)
    counts = {"matched": 0, "unmatched": 0, "ambiguous": 0, "paid": 0, "unpaid": 0}
    async with conn.transaction():
        for lid, (tid, ambiguous) in found.items():
            await conn.execute(
                "UPDATE expense_claim_line SET txn_id = $2, match = $3 WHERE id = $1",
                lid, tid, "ambiguous" if ambiguous else ("auto" if tid else None))
            counts["matched" if tid else "unmatched"] += 1
            counts["ambiguous"] += ambiguous

        # Payouts: an exact deposit after the trip, not already a payout
        used = {c["payout_txn_id"] for c in claims if c["payout_manual"] and c["payout_txn_id"]}
        for c in claims:
            if c["payout_manual"]:
                counts["paid" if c["payout_txn_id"] else "unpaid"] += 1
                continue
            percents = [float(r["tax_percent"]) for r in await conn.fetch(
                "SELECT DISTINCT tax_percent FROM payslip WHERE person_id = $1 "
                "AND tax_percent IS NOT NULL", c["person_id"])]
            wanted = expected_payouts(c["total_minor"], c["taxable_minor"], percents or [0])
            hit = next((t for a in wanted for t in sorted(txns, key=lambda t: t.booking_date)
                        if t.amount == a and t.id not in used
                        and c["trip_to"] <= t.booking_date <= c["trip_to"] + timedelta(days=150)),
                       None)
            if hit:
                used.add(hit.id)
            await conn.execute("UPDATE expense_claim SET payout_txn_id = $2 WHERE id = $1",
                               c["id"], hit.id if hit else None)
            counts["paid" if hit else "unpaid"] += 1

        await _rebuild_parts(conn, by_id)
    return counts


async def _rebuild_parts(conn, by_id: dict) -> None:
    """Split each payout deposit: what it pays back, by category, and the
    rest as income."""
    await conn.execute("DELETE FROM txn_part WHERE source = 'claim'")
    cats = {r["name"]: r["id"] for r in await conn.fetch("SELECT id, name FROM category")}
    other = cats.get("Other")
    expense_cat = {r["txn_id"]: r["category_id"] for r in await conn.fetch(
        """SELECT l.txn_id, tc.category_id FROM expense_claim_line l
           JOIN txn_category tc ON tc.txn_id = l.txn_id WHERE l.txn_id IS NOT NULL""")}
    deposits = await conn.fetch(
        """SELECT DISTINCT c.payout_txn_id AS id, t.amount_minor
           FROM expense_claim c JOIN txn t ON t.id = c.payout_txn_id""")
    for d in deposits:
        lines = await conn.fetch(
            """SELECT l.kind, l.amount_minor, l.txn_id, t.amount_minor AS paid
               FROM expense_claim l2 JOIN expense_claim_line l ON l.claim_id = l2.id
               LEFT JOIN txn t ON t.id = l.txn_id
               WHERE l2.payout_txn_id = $1 AND l.section = 'cost'""", d["id"])
        parts: dict[int | None, int] = {}
        for l in lines:
            if l["kind"] not in OUTLAY_KINDS or l["amount_minor"] <= 0:
                continue
            back = l["amount_minor"] if l["paid"] is None else min(l["amount_minor"], -l["paid"])
            cat = expense_cat.get(l["txn_id"]) or cats.get(DEFAULT_CATEGORY.get(l["kind"], ""), other)
            parts[cat] = parts.get(cat, 0) + back
        rest = d["amount_minor"] - sum(parts.values())
        rows = [(d["id"], n, "reimbursement", amount, cat)
                for n, (cat, amount) in enumerate(sorted(parts.items(), key=lambda x: str(x[0])), 1)]
        rows.append((d["id"], len(rows) + 1, None, rest, None))
        await conn.executemany(
            """INSERT INTO txn_part (txn_id, part_no, kind, amount_minor, category_id, source)
               VALUES ($1, $2, $3, $4, $5, 'claim')""", rows)


async def run(conn) -> dict:
    counts = await match_claims(conn)
    if counts:
        log.info("expense claims: %s", counts)
    return counts
