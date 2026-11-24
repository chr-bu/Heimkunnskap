# DNB Mastercard export format

Observed on 15 November 2026 from two real exports (73 and 107 transactions, the second with nine foreign purchases in JPY, SEK and DKK). Only the layout is recorded here; no real values.

## How it is obtained

From the credit card's transaction list in the DNB online bank, as Excel (`.xlsx`). The export covers one billing period at a time, not a calendar month. The current, unbilled period can be downloaded too, and grows until the invoice is issued.

## Layout

One sheet, `transaksjonsliste`. Six columns, header in row 1:

| Column | Header | Content |
| --- | --- | --- |
| A | Dato | Date. Stored as a datetime at 01:00, formatted `DD.MM.YYYY`. Use the date part only; the time is an artefact. |
| B | Beløpet gjelder | Description, usually `MERCHANT, CITY`. Some start with a 13-digit number. |
| C | Valuta | Empty for NOK purchases. For foreign purchases, text with the original amount and ISO currency code: `1234,56 JPY`. Comma as decimal separator, no thousands separator seen. |
| D | Kurs | Empty for NOK purchases. For foreign purchases, a number: NOK per one unit of the currency, also for JPY (not per 100). Rounded to three decimals. Sometimes empty for a foreign purchase too (seen for a euro purchase). |
| E | Inn | Credits (refunds, payments), positive number. One row in the sample. |
| F | Ut | Debits (purchases), positive number with two decimals. |

Row 2 is not a transaction: its description is `Skyldig beløp fra forrige faktura` (amount owed from the previous invoice), with the amount in column F. The parser must skip it.

Rows are sorted newest first. Each row has a value in either E or F, never both.

## Parser rules

- Skip the header and any row whose description is `Skyldig beløp fra forrige faktura`.
- Amount in øre: `Inn` as positive, `Ut` as negative, matching the ledger's sign convention.
- No card-holder column: purchases on an extra card cannot be told apart.
- No transaction reference: rows are de-duplicated by fingerprint (account, date, amount, description, occurrence counter). Re-uploading a period that has grown adds only the new rows.
- Foreign purchases: split `Valuta` at the last space into amount (comma decimal) and currency code, and store both with `Kurs`. `Ut` is already in NOK and is the amount that counts.
- Check: `Ut` equals amount × `Kurs` within the rate's rounding (half a unit of its last decimal, times the amount) plus 0.3%. For yen the rounding alone can be 0.8% (0.062 for 0.0624). With no `Kurs`, the foreign amount is kept and the rate left empty.
- The export shows no separate currency fee. Any surcharge DNB adds is inside the rate, so it cannot be split out per purchase as it can for Amex.
- Fail loudly on `Valuta` content that does not match `<amount> <three-letter code>`, rather than guess.

## Test fixture

`tests/fixtures/dnb_mastercard_sample.xlsx` has the same layout with invented merchants and amounts.
