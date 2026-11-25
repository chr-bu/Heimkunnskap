# DNB transaction texts

Layout only; no values. How DNB words the transactions that are not
income or spending, as sent through Enable Banking (`remittance_information`
joined into one description). Read by the DNB bank profile,
`heimkunnskap/banks/dnb.py`.

| Transaction | DNB's text | Rule |
| --- | --- | --- |
| Move between own accounts (both sides) | `Kontoregulering, Overføring Mellom Egne Konti` | `own:paired` |
| Money to or from the share savings account (ASK) | `... Spareavtale Til Aksjesparekonto` (standing order), `... Internoverføring Til Aksjesparekont` (one-off) | `own:paired` when both sides are seen; otherwise `saving:ask` (PSD2 does not show the ASK's cash account): a debit is saving, a credit saving taken out |
| Transfer typed in by account number | `Overføring Innland, <receiver's name>, Nettgiro` | own move when the name is a household member's |
| Move to or from the credit-line mortgage (rammelån) | any other own move with no other side: PSD2 does not show the rammelån | `loan:credit_line_transfer` |
| Card bill, American Express | `Giro, American Express` | `card:amex` |
| Card bill, SAS EuroBonus Mastercard (SEB Kort) | `Giro, Seb Kort` | `card:sas` |
| Card bill, DNB Mastercard | `Giro, Dnb Bank Asa, Efaktura` | `card:dnb_mastercard` |
| Card bill, Trumf Visa | `Giro, Norgesgruppen Trumf` | `card:trumf` |
| Fund purchase | `Fondshandel`, or `Giro, <fund>, Fast Oppdrag` | `saving:fund` |
| Fund or shares sold | `<fund> Overføring Innland, <fund>`, `Overføring Innland, ... Indeks ...`, `Verdipapir, ...`, `tilbakekjøp ... aksje` | `saving:sold` |

A fund is recognised by the words fund names are built from (`Indeks`, `Teknologi`, `Fond`, `Aksje...`, `Obligasjon...`, `Høyrente`, `Rentefond`, `Pengemarked`: `FUND_WORDS` in `banks/dnb.py`), not by a list of funds. A fund named otherwise needs its word added there. Platform fees (`Honorar fond`, `Plattformhonorar Fond`) are not purchases and stay fees.
| Dividend (income) | `Utbytte` | none |
| Mortgage instalment | `Lån, Lån <loan no>, ...` until October 2026; `Lån  <ref>, Lån <loan no>, ...` from November 2026 | `loan:instalment` |
| Payment to the rammelån | `Rammelån` or `Huslån` | `loan:credit_line` |
| Loan payout | `Lån, <borrower>` | `loan:payout` |
| Car finance | `Dnb Finans` | `loan:car` |

An instalment's text carries its split: `Avdrag Kr 6.000,00, Renter Kr
3.935,00, Omk. Kr 65,00`. From November 2026 the fee is left out of the
text; a remainder up to 500 kr is then taken as the fee.

Counterparty names sometimes come in 7-bit Norwegian: `{ | } [ \ ]` for
`æ ø å Æ Ø Å` ("L}nekassen").

A payment reference (KID) may lead the text ("600000006 Giro, Tibber") and
does not always come back in a later session; the fingerprint leaves it out.
