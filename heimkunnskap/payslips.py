"""Pay slips (PDF) for the tax estimate: what each employer has paid and
withheld so far this year, and this month's regular amounts to project the
rest of the year from.

Two layouts, recognised by their contents (docs/formats/):

  dfo       the state's payroll (DFØ), for state employers: "LØNNSSPESIFIKASJON",
            codes such as 1100 Regulativlønn and 8910 Forskuddstrekk
  layout_b  a private employer's payroll system: "Lønn/Trekk", "Utbetalt HIÅ"

The text layer is read with pypdf in layout mode, so each amount keeps its
column: the column header nearest to where an amount ends says whether it
is this period's amount, the year-to-date amount, or a basis or rate.

The national identity number and the account number on a slip are never
copied out of the PDF. The PDF itself goes to the raw store like any other
file.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date

DFO, LAYOUT_B = "dfo", "layout_b"

AMOUNT = re.compile(r"[−-]?\d{1,3}(?:[  ]\d{3})*,\d{2}")
HEADER_WORD = re.compile(r"Grunnlag|Gr\.lag|Sats|Beløp")
LAYOUT_B_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "mai", "jun", "jul", "aug", "sep", "okt", "nov", "des"], 1)}


class PayslipError(ValueError):
    """The PDF is not a pay slip this module can read."""


@dataclass
class Line:
    code: str | None
    text: str
    period: int | None          # øre
    ytd: int | None


@dataclass
class Payslip:
    layout: str
    employer: str
    employer_orgnr: str | None
    person_name: str
    period: date
    pay_date: date
    gross_ytd: int
    tax_base_ytd: int | None
    withheld_ytd: int
    pension_ytd: int
    salary: int
    benefits: int
    pension: int
    withholding: int
    tax_table: str | None
    tax_percent: float | None
    net_paid: int
    withheld_period: int = 0      # all tax withheld this month, table and percent
    lines: list[Line] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.employer}, {self.period:%m/%Y}: paid {self.net_paid / 100:,.2f}, "
                f"withheld so far {self.withheld_ytd / 100:,.2f}").replace(",", " ")


def to_minor(text: str) -> int:
    t = text.replace("−", "-").replace(" ", "").replace(" ", "").replace(",", ".")
    return round(float(t) * 100)


def pdf_text(content: bytes) -> str:
    import pypdf
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)
    except Exception as exc:                      # pypdf raises many kinds
        raise PayslipError(f"not a readable PDF: {exc}") from None


def detect(text: str) -> str | None:
    if "LØNNSSPESIFIKASJON" in text and "Forskuddstrekk" in text:
        return DFO
    if "Lønn/Trekk" in text and "Utbetalt HIÅ" in text:
        return LAYOUT_B
    return None


def _columns(lines: list[str]) -> list[tuple[str, int]]:
    """(header word, end position) of the amount columns, from the header
    row that names Beløp twice: this period's, then the year's."""
    for line in lines:
        if line.count("Beløp") == 2:
            return [(m.group(), m.end()) for m in HEADER_WORD.finditer(line)]
    raise PayslipError("pay slip: no column header with two Beløp columns")


def _amounts(line: str, columns) -> tuple[int | None, int | None, list[str]]:
    """This period's amount, the year-to-date amount, and the other numbers
    (bases, rates) on a line."""
    belop = [end for word, end in columns if word == "Beløp"]
    period = ytd = None
    others = []
    for m in AMOUNT.finditer(line):
        word, end = min(columns, key=lambda c: abs(c[1] - m.end()))
        if word == "Beløp" and abs(end - m.end()) <= 8:
            if end == belop[0]:
                period = to_minor(m.group())
            else:
                ytd = to_minor(m.group())
        else:
            others.append(m.group())
    return period, ytd, others


def _lines(text: str, code_re: re.Pattern, columns) -> tuple[list[Line], dict[str, Line]]:
    coded, totals = [], {}
    for raw in text.splitlines():
        if not AMOUNT.search(raw):
            continue
        period, ytd, _ = _amounts(raw, columns)
        head = AMOUNT.split(raw)[0].rstrip()
        m = code_re.match(head)
        if m:
            label = re.split(r"\s{2,}", m["text"].strip())[0]
            coded.append(Line(m["code"], label, period, ytd))
        else:
            label = re.split(r"\s{2,}", head.strip())[0]
            if label:
                totals[label] = Line(None, label, period, ytd)
    return coded, totals


def _find(lines: list[Line], pattern: str) -> Line | None:
    rx = re.compile(pattern, re.I)
    return next((l for l in lines if rx.search(l.text) or rx.search(l.code or "")), None)


def _value_under(lines: list[str], label: str) -> str:
    """The value printed under a header word, in the next non-empty line."""
    for i, line in enumerate(lines):
        pos = line.find(label)
        if pos >= 0:
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    chunk = nxt[max(0, pos - 3):].strip()
                    return re.split(r"\s{2,}", chunk)[0]
    raise PayslipError(f"pay slip: no value under {label!r}")


def parse_dfo(text: str) -> Payslip:
    lines = text.splitlines()
    columns = _columns(lines)
    coded, totals = _lines(text, re.compile(r"^\s*(?P<code>\d{3,4}[A-Z]?)\s+(?P<text>\S.*)$"),
                           columns)
    employer = _value_under(lines, "Firmanavn/ Avsender")
    orgnr = _value_under(lines, "Arbeidsgivernr.")
    period = _value_under(lines, "Periodenr")
    paid = _value_under(lines, "Dato")
    name = _value_under(lines, "Lønnsmottagers navn")
    try:
        period_d = date(int(period[:4]), int(period[4:6]), 1)
        pay_date = date(int(paid[:4]), int(paid[4:6]), int(paid[6:8]))
    except (ValueError, IndexError):
        raise PayslipError(f"pay slip: period or date not understood: {period!r}, {paid!r}") from None
    salary = _find(coded, r"^Regulativlønn")
    table_line = next((l for l in text.splitlines() if re.match(r"\s*8910\s", l)), "")
    pct_line = next((l for l in text.splitlines() if re.match(r"\s*8920\s", l)), "")
    table = re.search(r"\s(\d{4})\s", re.sub(r"^\s*8910", "", table_line))
    pct = re.search(r"\s(\d{1,2}[.,]\d{2})\s", re.sub(r"^\s*8920", "", pct_line))
    withholding = _find(coded, r"^8910$")
    pension = _find(coded, r"Pensjonstrekk")
    benefits = sum((l.period or 0) for l in coded
                   if re.search(r"Gruppeliv|Fordelsbeskatning|Fordel", l.text))
    gross, withheld = totals.get("Bruttolønn"), totals.get("Forskuddstrekk")
    net = re.search(r"Netto utbetalt\s+(" + AMOUNT.pattern + ")", text)
    if not (salary and withholding and gross and withheld and net):
        raise PayslipError("pay slip (DFØ): salary, withholding, gross or net not found")
    return Payslip(
        layout=DFO, employer=employer, employer_orgnr=orgnr, person_name=_name(name),
        period=period_d, pay_date=pay_date,
        gross_ytd=gross.ytd, tax_base_ytd=(totals.get("Skattetrekkgrunnlag") or Line(None, "", None, None)).ytd,
        withheld_ytd=-withheld.ytd, pension_ytd=-(pension.ytd or 0) if pension else 0,
        salary=salary.period or 0, benefits=benefits,
        pension=-(pension.period or 0) if pension else 0,
        withholding=-(withholding.period or 0),
        tax_table=table.group(1) if table else None,
        tax_percent=float(pct.group(1).replace(",", ".")) if pct else None,
        net_paid=to_minor(net.group(1)),
        withheld_period=-(withheld.period if withheld.period is not None
                          else sum(l.period or 0 for l in coded if l.code in ("8910", "8920"))),
        lines=coded)


def parse_layout_b(text: str) -> Payslip:
    lines = text.splitlines()
    columns = _columns(lines)
    coded, totals = _lines(text, re.compile(r"^\s*(?P<code>\d{3,5}(?:-\d)?)\s+-\s+(?P<text>\S.*)$"),
                           columns)
    employer = re.split(r"\s{2,}", next(l for l in lines if l.strip()).strip())[0]
    orgnr = re.search(r"Org\.nr\.:\s*([\d ]{9,11}\d)", text)
    period = _value_under(lines, "Periode")
    paid = _value_under(lines, "Betalingsdato")
    name = _value_under(lines, "Ansatt")
    m = re.match(r"([A-Za-zæøå]{3})\w*/(\d{4})", period)
    try:
        period_d = date(int(m.group(2)), LAYOUT_B_MONTHS[m.group(1).lower()], 1)
        d, mo, y = paid.split(".")
        pay_date = date(int(y), int(mo), int(d))
    except (AttributeError, KeyError, ValueError):
        raise PayslipError(f"pay slip: period or date not understood: {period!r}, {paid!r}") from None
    table = _value_under(lines, "Skattetabell")
    pct = _value_under(lines, "Prosenttrekk")
    salary = _find(coded, r"^Fastlønn$")
    holiday_pay = _find(coded, r"^Feriepenger")
    withholding = _find(coded, r"^Skattetrekk$")
    base, withheld = totals.get("Skattetrekkgrunnlag"), totals.get("Skattetrekk")
    fordel = totals.get("Sum: Fordel")
    net = re.search(r"Utbetalt\s+(" + AMOUNT.pattern + ")", text)
    if not (salary and withholding and base and withheld and net):
        raise PayslipError("pay slip (layout B): salary, withholding, totals or net not found")
    return Payslip(
        layout=LAYOUT_B, employer=employer, employer_orgnr=orgnr.group(1).replace(" ", "") if orgnr else None,
        person_name=_name(re.sub(r"\s*\(\d+\)$", "", name)), period=period_d, pay_date=pay_date,
        # the withholding base leaves out holiday pay, which is taxed all the same
        gross_ytd=base.ytd + ((holiday_pay.ytd or 0) if holiday_pay else 0),
        tax_base_ytd=base.ytd, withheld_ytd=-withheld.ytd, pension_ytd=0,
        salary=salary.period or 0, benefits=(fordel.period or 0) if fordel else 0, pension=0,
        withholding=-(withholding.period or 0),
        tax_table=table if re.fullmatch(r"\d{4}", table) else None,
        tax_percent=float(pct.replace(",", ".")) if re.fullmatch(r"\d{1,2},\d{2}", pct) else None,
        net_paid=to_minor(net.group(1)), withheld_period=-(withheld.period or 0), lines=coded)


def _name(text: str) -> str:
    """'Nordmann, Ola Eksempel' -> 'Ola Eksempel Nordmann'."""
    text = text.strip()
    if "," in text:
        last, first = [p.strip() for p in text.split(",", 1)]
        text = f"{first} {last}"
    return text


def name_key(name: str | None) -> str:
    """Lower-case words, sorted: the same person whatever the word order."""
    return " ".join(sorted((name or "").lower().replace(",", " ").split()))


def parse_text(text: str) -> Payslip:
    layout = detect(text)
    if layout == DFO:
        return parse_dfo(text)
    if layout == LAYOUT_B:
        return parse_layout_b(text)
    raise PayslipError("not a pay slip this app can read (DFØ or payroll layout B)")


def parse(content: bytes) -> Payslip:
    return parse_text(pdf_text(content))
