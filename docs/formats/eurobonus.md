# EuroBonus export (Excel), sas.no

Layout only; no values. Parsed by `heimkunnskap/eurobonus.py`.

On sas.no: My EuroBonus, Transactions, export to Excel. One sheet,
"Transactions", newest first, the account's whole history:

| Column | Content |
| --- | --- |
| Date | `YYYY-MM-DD`, as text |
| Activity | `Partner | Kind`; sometimes `Partner | Shop | Kind`; for flights the route, `Oslo - London | Basic Points`; points spent on an award have an empty kind: `Oslo - Bangkok | ` |
| Bonus Points | integer; negative when spent; empty for level-point rows |
| Level Points | integer; empty for most rows |

Partners seen: `Amex Norway Reward` (American Express, posted every few
days), `DNB Rewards` (DNB Mastercard Upgrade, posted once a month around
the 23rd, for purchases from the 25th of the month before),
`MasterCard Reward Norway` (SAS EuroBonus Mastercard, posted at the start
of the month for the month before), `NorgesGruppen` (Trumf, in blocks of
200 Trumf kroner at 13.5 points each), `Scandinavian Airlines System`,
`Level Points from DNB Rewards` and `Level Points from MasterCard Reward`
(level points only), shops through EuroBonus Online Shopping, and
anonymised partner codes (`a_<hex>`).

No member number or card number is in the file, though a transfer from
another member's account names that account's number in its activity.
The file goes to the raw store like any other upload; rows are matched
on day, activity, points and their place among equal rows, so each new
export adds only what is new.
