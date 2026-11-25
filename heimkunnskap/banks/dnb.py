"""DNB (Norway): its wording, as sent through Enable Banking.

Tuned against one household's accounts from 2021 to November 2026."""

from __future__ import annotations

import re

from . import BankProfile, rules

INSTALMENT_PARTS = re.compile(
    r"Avdrag\s+Kr\s+(?P<principal>[\d.]+,\d{2}).*?Renter\s+Kr\s+(?P<interest>[\d.]+,\d{2})"
    r"(?:.*?Omk\.\s+Kr\s+(?P<fees>[\d.]+,\d{2}))?", re.S)

FEE_MAX = 50000           # øre: the most an unnamed remainder can be and still be a fee


def _kr(text: str | None) -> int:
    return int(text.replace(".", "").replace(",", "")) if text else 0


def loan_split(description: str, amount_minor: int) -> tuple[int, int, int]:
    """(principal, interest, fees) of a loan payment, signed like the
    amount. An instalment's text holds the split ("Avdrag Kr 6.000,00,
    Renter Kr 3.935,00, Omk. Kr 65,00"); anything else, or parts that do
    not add up to the amount, counts as principal. Since November 2026
    the text leaves the fee out; a small remainder is then the fee."""
    m = INSTALMENT_PARTS.search(description or "")
    if m:
        principal, interest, fees = (_kr(m["principal"]), _kr(m["interest"]), _kr(m["fees"]))
        rest = abs(amount_minor) - principal - interest
        if m["fees"] is None and 0 < rest <= FEE_MAX:
            fees = rest
        if principal + interest + fees == abs(amount_minor):
            sign = -1 if amount_minor < 0 else 1
            return sign * principal, sign * interest, sign * fees
    return amount_minor, 0, 0


# Words Norwegian fund names are built from ("Global Indeks A",
# "Teknologi A", "Høyrente N"), so a fund is recognised without a list of
# funds
FUND_WORDS = (r"(?:Indeks|Teknologi|Fond|Fondet|Aksje\w*|Obligasjon\w*|H[øo]yrente"
              r"|Rentefond|Pengemarked)")


PROFILE = BankProfile(
    name="DNB",
    aspsp_names=("DNB",),
    # Both sides say "Overføring Mellom Egne Konti". Money to or from the
    # share savings account (ASK) names it: "Spareavtale Til Aksjesparekonto"
    # for a standing order, "Internoverføring Til Aksjesparekont" for a
    # one-off transfer.
    own_transfer=re.compile(r"Mellom Egne Konti|Aksjesparekont", re.I),
    # A transfer typed in by account number: "Overføring Innland, <name>, Nettgiro"
    transfer_to_name=re.compile(r"^Overf[øo]ring Innland, (?P<name>[^,]+)", re.I),
    # PSD2 does not show DNB's credit-line mortgage (rammelån), so a move
    # between own accounts whose other side is missing is a move to or from it
    unpaired_own_transfer="loan:credit_line_transfer",
    # PSD2 does not show the ASK's cash account either, so a move to or from
    # it with no other side is saving, never the mortgage
    unpaired_saving=rules(("saving:ask", r"Aksjesparekont")),
    card_bills=(
        ("amex", re.compile(r"Giro, American Express\b", re.I)),
        ("sas", re.compile(r"Giro, Seb Kort\b", re.I)),
        ("dnb_mastercard", re.compile(r"Giro, Dnb Bank Asa, Efaktura", re.I)),
        ("trumf", re.compile(r"Giro, Norgesgruppen Trumf\b", re.I)),
    ),
    # Fund purchases: through the fund platform, or a standing order to a
    # fund ("Giro, <fund>, Fast Oppdrag")
    saving=rules(("saving:fund", rf"\bFondshandel\b|\b{FUND_WORDS}\b.*\bFast Oppdrag\b")),
    # Money back from funds and shares. A fund redemption reads
    # "<fund> Overføring Innland, <fund>"; fund names are recognised by the
    # words funds are named with, not by a list of funds.
    saving_out=rules(
        ("saving:sold", r"\bIndeks\b|^Verdipapir, |\btilbakekj[øo]p\b.*\baksje"
                        rf"|^[^,]*\b{FUND_WORDS}\b[^,]* Overf[øo]ring Innland, ")),
    dividend=re.compile(r"\bUtbytte\b", re.I),
    # "Lån, Lån 1111.22.33333, ..." until October 2026; from November DNB
    # puts a reference after the first word: "Lån  111111, Lån 1111.22.33333, ..."
    loans=rules(
        ("loan:instalment", r"^Lån(?:\s+\d+)?,\s*Lån \d"),
        # The rammelån; payments to it are also saved as "Huslån"
        ("loan:credit_line", r"Rammel[åa}]n|\bHusl[åa}]n\b"),
        # Money paid out of a loan into an account, or back: "Lån, <borrower>"
        ("loan:payout", r"^Lån, (?!Lån )"),
        # DNB's car finance; the e-invoice does not split interest out
        ("loan:car", r"\bDnb Finans\b"),
    ),
    loan_split=loan_split,
    iso646_names=True,
)
