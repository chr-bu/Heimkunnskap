"""Synthetic expense claims, laid out the way `pdftotext -raw` (or OCR)
reads Unit4 travel system claims (docs/formats/unit4-expense-claim.md). No real values."""


def abroad(claim_no="100001", hotel="10000,00", total="15800,00", tax_free="14900,00",
           taxable="900,00", costs_sum="14200,00") -> str:
    return f"""Utenlandsreise - Statens regulativ ID : {claim_no}
Ansatt Firma
Ola Eksempel Nordmann (99999)
Eksempelveien 1
1234 STED
EKSEMPELETATEN
Fra tidspunkt Oppdragssted Tidssone Til tidspunkt
04.05.2026 - 08:00 Frankrike - Paris +1 06.05.2026 - 22:00
Hensikt med reisen/utlegget Deltakelse Eksempelkonferansen
Reiserute Sted-OSL-CDG-Paris-CDG-OSL-Sted
Stilling 0000 Rådgiver-100% 000000 - USPESIFISERT
DIETTER OG FRATREKK
Dato Kostnadstype Valuta Valutabeløp
Merknad Antall Pris Beløp Beløp før skatt
04.05.2026 Diett over 12 timer hotell uten frokost u/kok Frankrike -
Paris
EUR 10,000000 180,00
Eksempelhotellet 2,0 900,00 1800,00 1800,00
04.05.2026 Fratrekk lunsj Frankrike - Paris EUR 10,000000 -20,00
1,0 -200,00 -200,00 -200,00
Sum Dietter og fratrekk (NOK) 1600,00 1600,00
KOSTNADER
Dato Kostnadstype Valuta Valutabeløp
Merknad Antall Pris Beløp Beløp før skatt
06.05.2026 Hotellregning utland EUR 10,000000 1000,00
Fratrekk på 10 Euro for minibar, ikke dekket av
arbeidsgiver
1,0 {hotel} {hotel} {hotel}
04.05.2026 Fly utland
_ 1,0 2500,00 2500,00 2500,00
04.05.2026 Taxi utland EUR 10,000000 50,00
Taxi fra CDG til hotell 1,0 500,00 500,00 500,00
06.05.2026 Tog innland
Flytog OSL-Sted 1,0 200,00 200,00 200,00
04.05.2026 Kompensasjonstillegg utland
2,0 500,00 1000,00 1000,00
Sum Kostnader (NOK) {costs_sum} {costs_sum}
Totalsum (NOK) {total} {total}
INFORMASJON - BELØP
Skattefri del {tax_free}
Skattebelagt del {taxable}
Sum (NOK) {total}
"""


def domestic_ocr() -> str:
    """A scanned domestic claim as tesseract reads it: "ø" comes out as "g"
    or "@", and the route lines of the mileage claim stay in."""
    return """Innenlandsreise - Statens regulativ ID : 100002
Ansatt Firma
Ola Eksempel Nordmann (99999) EKSEMPELETATEN
Eksempelveien 1
1234 STED
Fra tidspunkt Oppdragssted Tidssone Til tidspunkt
10.03.2026 - 07:00 Norge +1 11.03.2026 - 17:00
Hensikt med reisen/utlegget Deltakelse pa samling
DIETTER OG FRATREKK
Dato Kostnadstype Valuta Valutabelgp
Merknad Antall Pris Belgp _Belgp for skatt
10.03.2026 Diett over 12 timer hotell med frokost u/kok Norge
Eksempel Hotell 1,0 1000,00 1000,00 1000,00
10.03.2026 Fratrekk middag Norge
1,0 -500,00 -500,00 -500,00
Sum Dietter og fratrekk (NOK) 500,00 500,00
KOSTNADER
Dato Kostnadstype Valuta Valutabelgp
Merknad Antall Pris Belgp _Belgp for skatt
10.03.2026 Bilgodtgjgrelse elbil innland
100,0 5,30 530,00 530,00
Eksempelveien 1, Sted, Norway -> Eksempel Hotell, Hotellvegen, @yer, Norway
Bompenger enkel passering
Bomstasjon Nord 1,0 20,00 20,00 20,00
Bompenger enkel passering
Bomstasjon Sor 1,0 0,00 0,00 0,00
11.03.2026 P-avgift innland under kr 500
Parkering sentrum 1,0 150,00 150,00 150,00
Sum Kostnader (NOK) 700,00 700,00
Totalsum (NOK) 1200,00 1200,00
INFORMASJON - BEL@P
Skattefri del 1000,00
Skattebelagt del 200,00
Sum (NOK) 1200,00
"""


def outlay() -> str:
    """An outlay claim (Utlegg), not a trip: no Totalsum, only the summary."""
    return """Utlegg - innland ID : 100003
Ansatt Firma
Ola Eksempel Nordmann (99999)
Eksempelveien 1
1234 STED
EKSEMPELETATEN
Fra tidspunkt Til tidspunkt
17.03.2026 17.03.2026
Hensikt med reisen/utlegget Abonnement fagblad
KOSTNADER
Dato Kostnadstype Valuta Valutabeløp
Merknad Antall Pris Beløp Beløp før skatt
17.03.2026 Refusjon abonnement aviser, tidsskrifter, bøker,
medlemsavgift i foreninger
Etter avtale med leder 1,0 1500,00 1500,00 1500,00
Sum Kostnader (NOK) 1500,00 1500,00
INFORMASJON - BELØP
Skattefri del 1500,00
Skattebelagt del 0,00
Sum (NOK) 1500,00
"""
