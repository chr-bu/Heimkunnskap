"""Synthetic pay slip text, laid out the way pypdf's layout mode reads the
real slips (docs/formats/dfo-payslip.md, layout-b-payslip.md): amounts
right-aligned under their column headers. No real values."""


def kr(minor: int, nbsp: bool = False, minus: str = "-") -> str:
    sign = minus if minor < 0 else ""
    whole, ore = divmod(abs(minor), 100)
    sep = " " if nbsp else " "
    return f"{sign}{whole:,}".replace(",", sep) + f",{ore:02d}"


def place(row: str, text: str, end: int) -> str:
    """Put text so that it ends at column end."""
    row = row.ljust(end)
    return row[:end - len(text)] + text + row[end:]


def dfo(salary=5000000, extra=0, pension=-100000, table=-1500000, pct=-60000, gross_ytd=55000000,
        base_ytd=48000000, withheld_ytd=-16000000, pension_ytd=-1100000, net=None,
        period="202611", paid="20261112") -> str:
    net = net if net is not None else salary + extra + pension + table + pct
    head = " Kode   Lønn/trekk".ljust(52) + "Tab / %".ljust(20) + "Denne perioden".ljust(40) + "Hittil i år"
    cols = "".ljust(70) + "Ant/ Gr.lag" + "Sats".rjust(12) + "Beløp".rjust(15) + "Ant/ Gr.lag".rjust(21) + "Beløp".rjust(18)
    p_end, y_end = cols.index("Beløp") + 5 + 4, cols.rindex("Beløp") + 5 + 4
    rows = []
    for code, text, tab, per, ytd in [
            ("1100", "Regulativlønn (217)", "", salary, salary * 11),
            ("1100S", "ER: Regulativlønn", "", extra, extra),
            ("1163", "Gruppelivsforsikring", "", 15000, 165000),
            ("3121", "Fordelsbeskatning elektroniske tjenester", "", 36600, 402600),
            ("7200", "Pensjonstrekk SPK 2%", "", pension, pension_ytd),
            ("8910", "Forskuddstrekk", "8888", table, withheld_ytd - pct * 2),
            ("8920", "Forskuddstrekk %", "48.00", pct, pct * 2)]:
        if per == 0 and ytd == 0:
            continue
        row = f" {code:<9} {text}".ljust(52) + tab
        row = place(row, kr(per, nbsp=True), p_end)
        rows.append(place(row, kr(ytd, nbsp=True), y_end))
    totals = [place(f"           {label}".ljust(60), kr(v, nbsp=True), y_end) for label, v in [
        ("Feriepengegrunnlag", base_ytd + 1000000), ("Skattetrekkgrunnlag", base_ytd),
        ("Bruttolønn", gross_ytd), ("Forskuddstrekk", withheld_ytd)]]
    return "\n".join([
        "".ljust(83) + "Firmanavn/ Avsender",
        "".ljust(83) + "EKSEMPELETATEN",
        "",
        "Lønnsmottagers navn".ljust(82) + "Arbeidsgivernr.".ljust(32) + "Periodenr".ljust(22) + "Dato",
        "Nordmann, Ola Eksempel".ljust(83) + "999999999".ljust(32) + period.ljust(22) + paid,
        "Eksempelveien 1".ljust(83) + "Fødselsnr.".ljust(31) + "Ansatt nr.".ljust(22) + "Bankkontonr.",
        "1234 STED".ljust(83) + "00000000000".ljust(29) + "1".ljust(22) + "00000000000",
        "", "LØNNSSPESIFIKASJON", "", head, cols, ""] + rows + [""] + totals + [
        "", "".ljust(72) + "Netto utbetalt" + kr(net, nbsp=True).rjust(28) + "    Utbetalt til           00000000000"])


def layout_b(salary=6000000, benefits=91400, table=-2000000, base_ytd=50000000, holiday_ytd=7000000,
            withheld_ytd=-18000000, net=None, period="Okt/2026", paid="20.10.2026") -> str:
    minus = "−"
    net = net if net is not None else salary + table - 45000
    cols = "".ljust(90) + "Grunnlag" + "Sats".rjust(27) + "Beløp".rjust(23) + "Grunnlag".rjust(25) + "Beløp".rjust(25)
    p_end, y_end = cols.index("Beløp") + 5 - 1, cols.rindex("Beløp") + 5 - 1
    g_end = cols.index("Grunnlag") + 8

    def row(label, per=None, ytd=None, basis=None):
        r = f" {label}"
        if basis is not None:
            r = place(r, kr(basis, minus=minus), g_end)
        if per is not None:
            r = place(r, kr(per, minus=minus), p_end)
        if ytd is not None:
            r = place(r, kr(ytd, minus=minus), y_end)
        return r
    return "\n".join([
        "".ljust(67) + "Eksempel AS" + "Org.nr.: 999 999 999".rjust(110),
        "".ljust(67) + "Eksempelgata 1",
        "",
        "         Kari Eksempel Nordmann".ljust(101) + "Periode".ljust(50) + "Betalingsdato",
        "         Eksempelveien 1".ljust(101) + period.ljust(50) + paid,
        "",
        "Ansatt".ljust(80) + "Fødselsnr.".ljust(41) + "Skattetabell".ljust(40) + "Prosenttrekk",
        "Kari Eksempel Nordmann (1)".ljust(80) + "00000000000".ljust(41) + "7777".ljust(40) + "47,00",
        "",
        "  Lønn/Trekk".ljust(105) + "Denne periode".ljust(66) + "Hittil i år",
        cols,
        row("0404-1 - Ferietrekk", ytd=-holiday_ytd + 300000),
        row("10010 - Fastlønn", per=salary, ytd=salary * 10),
        row("1500 - Feriepenger opptjent i fjor", ytd=holiday_ytd),
        row("".ljust(75) + "Sum: Lønn", per=salary, ytd=salary * 10 + 300000),
        row("5090 - Kantinetrekk", per=-45000, ytd=-450000),
        row("7990 - Skattetrekk", per=table, ytd=withheld_ytd + 23000, basis=salary + benefits),
        row("7992 - Skattetrekk prosent", ytd=-23000),
        row("2000 - Livsforsikring - gruppeliv", per=30000, ytd=300000),
        row("31201 - Fordel EKT", per=61400, ytd=614000),
        row("".ljust(75) + "Sum: Fordel", per=benefits, ytd=benefits * 10),
        "",
        "  Totaler".ljust(105) + "Denne periode".ljust(66) + "Hittil i år",
        row("Skattetrekkgrunnlag", per=salary + benefits, ytd=base_ytd),
        row("Skattetrekk", per=table, ytd=withheld_ytd),
        "",
        "".ljust(116) + "Utbetalt" + kr(net, minus=minus).rjust(22) + "               Utbetalt HIÅ" + kr(net * 10, minus=minus).rjust(18),
    ])
