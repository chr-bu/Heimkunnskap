# Expense claim (reiseregning), Unit4 travel system, PDF

Layout only; no values. Parsed by `heimkunnskap/claims.py`.

Two pages. Some PDFs have a text layer (`pdftotext -raw` reads it in rows,
with clean words; `-layout` splits words, "Frank rik e"), others are scans
(`tesseract --psm 6` at 300 dpi reads them in the same rows; "ø" comes out
as "g" or "@", digits are reliable). Either way, one row of the claim per line.

Header:

    <Innenlandsreise|Utenlandsreise> - Statens regulativ ID : <number>
    Ansatt Firma
    <Name> (<employee number>) [<employer>]
    <address, two lines>                      never copied
    Fra tidspunkt Oppdragssted Tidssone Til tidspunkt
    <dd.mm.yyyy - hh:mm> <place> <+1> <dd.mm.yyyy - hh:mm>
    Hensikt med reisen/utlegget <purpose>
    Reiserute, Beskjed, Stilling, Produkt, Koststed, Felles, Oppgave

Sections "DIETTER OG FRATREKK" (allowances: per diem, deductions for meals
provided) and "KOSTNADER" (costs). Each item:

    <dd.mm.yyyy> <cost type> [<currency> <rate> <foreign amount>]
    [type wrapped onto the next line] [<currency> <rate> <foreign amount>]
    [<note>, possibly over two lines] <quantity> <price> <amount> <amount before tax>

A mileage item (Bilgodtgjørelse) is followed by its route ("address ->
address", never copied) and by undated toll rows ("Bompenger enkel
passering" on one line, then "<toll station> 1,0 <price> <amount> <amount>").
A note may begin like a cost type ("Fratrekk på 5 Euro for minibar"); below
a dated line, text is only a wrap or a note.

Totals: "Sum Dietter og fratrekk (NOK)", "Sum Kostnader (NOK)",
"Totalsum (NOK)", then "INFORMASJON - BELØP": "Skattefri del",
"Skattebelagt del", "Sum (NOK)". The parser refuses a claim whose rows do
not add up to these.

An outlay claim that is not a trip ("Utlegg - innland", e.g. a refunded
subscription or membership) has the same layout with only KOSTNADER, dates
without times in the header, and no Totalsum line: its total is the
summary's "Sum (NOK)". Subscriptions are matched to a card payment of the
exact amount, up to 60 days before the claim's date.

Payout: the claim is paid with salary, as a separate deposit ("Lønn,
<employer>"), of the total less tax withheld at the pay slip's percentage on
the taxable part, in whole kroner, rounded down.
