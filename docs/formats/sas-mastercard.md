# SAS EuroBonus Mastercard export format

Observed on 16 November 2026 from one real export (82 transactions over 13 months, two cards, purchases in USD, SGD, JPY and DKK). Only the layout is recorded here; no real values.

## Why a file at all

The card also comes through Enable Banking, but the bank shares only about 13 months of history. The export reaches further back, so it fills in the older history. It is merged into the bank feed's account, not kept as a second card: only rows from before the feed's first transaction are added. From that day on the feed is the record. Its texts differ from the export's (`Inbetalning` against `Innbetaling`, Swedish fee names against Norwegian ones), so taking both would count each purchase twice.

## How it is obtained

From SEB Kort's pages for the SAS EuroBonus Mastercard: the transaction list, exported as Excel (`.xlsx`) for a chosen date range. The file name gives the range, newest date first: `transactions-DD.MM.YYYY-to-DD.MM.YYYY.xlsx`.

## Layout

One sheet, `Transaksjoner`, in sections:

1. Row 1: `Transaksjonseksport` in A, the export time in G.
2. **The account's own events**: a row `Totalt andre hendelser`, the header row, then payments in (`Innbetaling`) and fees (`Kortkontingent`, `Gebyr erstatningskort`, `Avgift uttak minibank`). Then a row with `Saldo hendelser` in C and the sum of all card sections in G, and `Totalbeløp` in A with the section total in G. That total includes the `Saldo hendelser` amount.
3. **One section per card**: a row with the masked card number in A (`540185******1234`) and the holder's name in B, a row `Kjøp/uttak`, the header row, the purchases, and `Totalbeløp` with the section total.

The header row:

| Column | Header | Content |
| --- | --- | --- |
| A | Dato | Purchase date, a datetime at midnight |
| B | Bokført | Booking date. The bank feed's booking date matches this one |
| C | Spesifikasjon | Merchant or event text |
| D | Sted | Place, usually a city; empty for the account's events |
| E | Valuta | ISO currency code; `NOK` for NOK purchases |
| F | Utl. beløp | Amount in the foreign currency; `0` for NOK |
| G | Beløp | NOK amount. Purchases and fees positive, payments and refunds negative |

A foreign purchase is followed by a row with only A filled in: `Valutakurs: 10.336683 Valutapåslag inngår med 2,00 %`. The rate is NOK per unit (also for JPY), and includes the 2% markup, so foreign amount × rate gives the NOK amount.

## Parser rules

- Booking date from `Bokført`; the purchase date is kept as the value date.
- Amount in øre with the sign flipped: purchases negative, payments positive, as in the ledger and the bank feed.
- The rate row belongs to the row above it. Foreign amount × rate must give the NOK amount within 0.3% plus one øre.
- Each section's rows, plus `Saldo hendelser` in the first, must add up to its `Totalbeløp`. A file without a closing total is refused.
- The holder's name goes in `card_holder`, and `Sted` in `extra.place`. The masked card number is not stored.
- No reference column: rows are de-duplicated by fingerprint against the feed account's key.
