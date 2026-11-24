# American Express export format

Observed on 15 November 2026 from one real export of the American Express SAS EuroBonus Elite Credit Card (16 transactions). Only the layout is recorded here; no real values.

## How it is obtained

From the account activity page on americanexpress.com, as Excel (`.xlsx`). The export covers one billing period, for example 27 October to 26 November. The current, open period can be downloaded and grows until the statement is issued.

## Sheet `Transaksjonsdetaljer`

Rows 1 to 6 are a header block: the card product and period in B1 (`American Express® SAS Eurobonus Elite Credit Card / 27 Oct, 2026 til 26 Nov, 2026`), the main card member's name in A3 and a masked account number in A5 (`XXXX-XXXXXX-99999`). Column headers are in row 7 and transactions start in row 8.

| Column | Header | Content |
| --- | --- | --- |
| A | Dato | Date as text in US order, `MM/DD/YYYY`. Not `DD/MM`. |
| B | Beskrivelse | Merchant description |
| C | Kortmedlem | Card holder name. Tells main card and extra card apart. |
| D | Kontonummer | Masked card number of that card holder |
| E | Beløp | Amount in NOK as a number. Positive for charges, negative for payments and credits. |
| F | Utvidede detaljer | Free text, often empty. Holds airline ticket details (route, class, passenger, ticket number) and, for foreign purchases, a line such as `Foreign Spend Amount: 9.999,99 EUROPEAN UNION EURO Commission Amount: 999.9 Currency Exchange Rate: 9.999`. |
| G | Opptrer på din kontoutskrift som | Description as on the statement |
| H | Adresse | Merchant address, may contain line breaks |
| I | Sted | Merchant town |
| J | Postnummer | Merchant postcode |
| K | Land | Merchant country, in English |
| L | Referanse | Unique reference, 23 characters starting with `AT` |
| M | Kategori | Amex category. Mostly `Miscellaneous-Other`, so of little use. |

## Sheet `Transaksjonssammendrag`

Totals for the period: previous balance, payments and credits (negative), charges, and total balance. A table below gives the same per card holder, listing both the main and the extra card even when one has no activity.

## Parser rules

- Read transactions from row 8 of `Transaksjonsdetaljer`. Parse dates as `MM/DD/YYYY`.
- Ledger amount in øre is the negative of `Beløp`: a charge becomes a negative amount, a payment or refund positive.
- De-duplicate on `Referanse`, which is unique per transaction. Re-uploading a grown period adds only the new rows.
- Keep `Kortmedlem` and the masked card number, so spending and points can be shown per card holder.
- From `Utvidede detaljer`, extract the foreign amount, currency, exchange rate and commission when present. The currency is a full English name (`EUROPEAN UNION EURO`), not a code, Euro, US dollar and Singapore dollar have been seen. Known names map to ISO codes (`AMEX_CURRENCIES`); an unknown name does not stop the import, since the NOK amount is what counts: the foreign amount is kept with the name as text, and the upload result names it so it can be added. Numbers there use Norwegian notation in the amount (`9.999,99`) but a decimal point in commission and rate, so parse each field on its own.
- Do not store passenger names or ticket numbers from `Utvidede detaljer` in the ledger. They stay in the raw store only.
- Check after import: the sum of positive amounts must equal `Debiteringer`, and the sum of negative amounts must equal `Betalinger og kreditter` in the summary sheet. Both matched in the sample.

## What it adds to the app

- The commission on foreign purchases is visible per transaction, so the cost of using each card abroad can be compared.
- Airline purchases are recognisable, which matters for EuroBonus earn rules.

## Test fixture

`tests/fixtures/amex_sample.xlsx` has the same layout with invented names, merchants and amounts.
