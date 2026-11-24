# trumf.no purchases and receipts

Layout only; no values. Read by `heimkunnskap/trumf.py`. There is no
official API: these are the requests the trumf.no web site makes in the
browser (seen 23 November 2026), so they may change without notice.

## Login

trumf.no is a Next.js site signed in through Auth.js against
id.trumf.no. The login is the cookie `__Secure-authjs.session-token`,
split into `__Secure-authjs.session-token.0`, `.1`, ... when it is large.
It lasts a year from its last use and is renewed (sent again in
`Set-Cookie`) as it is used. Nothing else is needed: the site turns it
into a NorgesGruppen access token on the server.

`GET /api/auth/session` returns JSON with `user`, `expires`,
`accessToken`, `idToken`, `memberId` and `accessTokenExpired` while the
login holds, and `{}` when it has ended.

## The purchase list

`GET /trumf-profil/kvitteringer` with the header `RSC: 1` returns the
page's React Server Components payload (`text/x-component`): lines of
`id:json`. One of them holds `"transactionsData":[...]`, the last 12
months, grouped; each purchase is an object with:

| Field | Content |
| --- | --- |
| `batchId` | Trumf's id for the purchase; digits, or a date and a number for web shops |
| `transaksjonsTidspunkt` | time, `$D` + ISO 8601 UTC |
| `transaksjonKategori` | `ACCRUAL` (a purchase earning bonus) or `CONSUME` (bonus used: the transfers to EuroBonus) |
| `partnerId` | chain: `KIWI`, `MENY`, `SPAR`, `JOK` (Joker), `NORL` (Nærbutikken), `MEGR`, `VIAT` (Trumf Netthandel), `EB` (EuroBonus) |
| `beskrivelse` | the store's name |
| `belop`, `bonus`, `totaleBesparelser` | kroner as numbers; bonus negative for `CONSUME` |
| `harKvittering` | whether a receipt can be opened |
| `medlemId`, `kilde`, `filterCategory`, `reklamasjonsStatus`, `bonusberegningTidspunkt` | also present |

`$undefined` stands for a missing value. Without `RSC: 1` the page may
carry the same payload inside its HTML (below); the fetcher tries the HTML
first and asks for the payload if the data is not there.

## A receipt

`GET /trumf-profil/kvitteringer/<batchId>?transactionType=ACCRUAL&timestamp=<transaksjonsTidspunkt>&description=<beskrivelse>`
returns an HTML page. Its data is in
`<script>self.__next_f.push([1,"..."])</script>` calls: JSON strings
that join into the same kind of payload. It holds
`"purchaseDetails":{...}`:

| Field | Content |
| --- | --- |
| `batchId`, `kvitteringsId`, `ordreId` | ids |
| `belop`, `bonus`, `transaksjonsTidspunkt`, `beskrivelse`, `partnerId` | as in the list |
| `varelinjer` | the lines, below |
| `bonusreglerSum`, `momsreglerSum`, `besparelserSum` | totals per bonus rule, VAT rate and discount |

Each line (`varelinjer[]`): `produktBeskrivelse`, `ean` (often null for
web shops), `antall` (count, or weight for `enhetsType` `KG`),
`enhetsType`, `belop`, `bonus`, `enhetsBonus`, `bonusProsent`,
`momsProsent`, `ukjentVare`, `bonusregler[]` (`bonusregelId`, `bonus`,
`bonusregelBeskrivelse`: why the line earned its bonus), `besparelser[]`
(discounts), `reklamasjonsStatus`, `reklamasjonsSum`, `varelinjeGuid`.

## What is kept

`trumf_txn` (one row per purchase, with the list's object as `raw`) and
`trumf_line` (one row per receipt line). The list and each receipt also
go to the raw store. Each purchase is matched to the card payment it was:
the same amount, booked the same day or up to four days later, the
store's chain in the bank text when two payments fit.

The member id is in every row Trumf sends; it stays in the raw data and
is not shown in the report views.
