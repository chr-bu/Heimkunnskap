"""Bank profiles: how each bank writes the transactions that are not income
or spending.

Banks reached through Enable Banking all send the same fields, but each
writes its own text: a move between own accounts, a card bill paid, a fund
purchase, a loan instalment. A profile holds one bank's patterns, and the
transfer pairing (pairing.py) reads each transaction with the profile of the
bank its account is in, found by the account's aspsp_name (the bank's name
in Enable Banking).

A bank without a profile still syncs. Its transactions are then income or
spending, apart from what the shared rules in pairing.py catch (card bills
seen from the card's side, student loans, reimbursements). To add a bank,
write heimkunnskap/banks/<bank>.py with a PROFILE, list it in PROFILES
below, and add tests with made-up transactions in the bank's wording
(tests/test_banks.py). CONTRIBUTING.md says more.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

Rules = tuple[tuple[str, re.Pattern], ...]


def rules(*pairs: tuple[str, str]) -> Rules:
    """(rule name, regex) pairs, compiled case-insensitively."""
    return tuple((name, re.compile(pattern, re.I)) for name, pattern in pairs)


def no_split(description: str, amount_minor: int) -> tuple[int, int, int]:
    """A loan payment whose text does not split it: all principal."""
    return amount_minor, 0, 0


@dataclass(frozen=True)
class BankProfile:
    name: str
    # The bank's name in Enable Banking, as on the account (aspsp_name)
    aspsp_names: tuple[str, ...]
    # A move between two of the household's accounts in this bank
    own_transfer: re.Pattern | None = None
    # A transfer to an account in a household member's own name, with the
    # name in the group "name"
    transfer_to_name: re.Pattern | None = None
    # The rule for an own move whose other side is not found. None: an own
    # transfer without a partner (the review page lists it). A bank that
    # hides an account type from PSD2 (DNB's credit-line mortgage) can name
    # a loan rule instead.
    unpaired_own_transfer: str | None = None
    # Own moves that are saving when their other side is not found, checked
    # before unpaired_own_transfer: a move to or from an account PSD2 does
    # not show that holds savings (DNB's share savings account, ASK). A
    # debit is money put away, a credit money taken back out.
    unpaired_saving: Rules = ()
    # Card bills paid from this bank: card brand (as in pairing.CARDS) to
    # the regex for the bill's text
    card_bills: tuple[tuple[str, re.Pattern], ...] = ()
    # Money put away (debits) and money coming back from it (credits)
    saving: Rules = ()
    saving_out: Rules = ()
    # A dividend, which stays income even when it comes from savings
    dividend: re.Pattern | None = None
    # Loan payments and payouts
    loans: Rules = ()
    # (principal, interest, fees) of a loan payment, from its text
    loan_split: Callable[[str, int], tuple[int, int, int]] = no_split
    # Counterparty names in 7-bit Norwegian ("L}nekassen")
    iso646_names: bool = False
    notes: tuple[str, ...] = field(default=())


NO_PROFILE = BankProfile(name="(no profile)", aspsp_names=())


def _profiles() -> list[BankProfile]:
    from . import dnb
    return [dnb.PROFILE]


PROFILES: list[BankProfile] = _profiles()


def for_bank(aspsp_name: str | None) -> BankProfile:
    """The profile for a bank, by its name in Enable Banking; NO_PROFILE
    for a bank without one and for accounts read from files (cards)."""
    for p in PROFILES:
        if aspsp_name in p.aspsp_names:
            return p
    return NO_PROFILE
