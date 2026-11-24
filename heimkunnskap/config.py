"""Environment-driven configuration. Everything specific to the household
lives in .env (see .env.example), never in the repository."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _list(name: str) -> tuple:
    return tuple(x.strip() for x in os.environ.get(name, "").replace(";", ",").split(",")
                 if x.strip())


def _banks() -> dict:
    """BANKS="DNB=DNB:NO,SAS=SAS EuroBonus Mastercard:NO": short name to
    Enable Banking's ASPSP name and country, for the consent links."""
    raw = os.environ.get("BANKS", "DNB=DNB:NO,SAS=SAS EuroBonus Mastercard:NO")
    banks = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        short, _, rest = item.partition("=")
        name, _, country = rest.rpartition(":")
        if short.strip() and name.strip() and country.strip():
            banks[short.strip()] = (name.strip(), country.strip().upper())
    return banks


@dataclass(frozen=True)
class Settings:
    # Database: the owner login writes, the read-only login only sees the
    # report schema (sql/002_report.sql)
    database_url: str = os.environ.get("DATABASE_URL", "")
    database_url_ro: str = os.environ.get("DATABASE_URL_RO", "")
    timezone: str = os.environ.get("TIMEZONE", "Europe/Oslo")

    # Enable Banking (docs/enable-banking.md). The private key is a file, mounted
    # read-only into the containers, never an environment variable.
    eb_app_id: str = os.environ.get("EB_APP_ID", "").strip()
    eb_key_file: str = os.environ.get("EB_KEY_FILE", "/run/secrets/eb-key.pem")
    eb_api: str = os.environ.get("EB_API", "https://api.enablebanking.com").rstrip("/")
    banks: dict = field(default_factory=_banks)
    consent_days: int = _int("CONSENT_DAYS", 180)
    consent_warn_days: int = _int("CONSENT_WARN_DAYS", 14)

    # Sync: once a night at SYNC_AT local time. A full import (right after
    # BankID) reaches FULL_HISTORY_DAYS back; a nightly one starts
    # OVERLAP_DAYS before the newest booked transaction already stored.
    sync_at: str = os.environ.get("SYNC_AT", "02:30")
    full_history_days: int = _int("FULL_HISTORY_DAYS", 1825)
    first_sync_days: int = _int("FIRST_SYNC_DAYS", 89)
    overlap_days: int = _int("OVERLAP_DAYS", 10)

    # The sole proprietorship's income account, as its account number
    # (BBAN, e.g. 12345678903) or IBAN. Every deposit there is business income.
    business_income_accounts: tuple = field(
        default_factory=lambda: _list("BUSINESS_INCOME_ACCOUNTS"))

    # Web app: the public address (for the BankID redirect), the key that
    # signs its session cookie, and the networks it answers (CIDRs; Apache
    # restricts too). The MCP path has its own rules.
    public_url: str = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    web_secret_key: str = os.environ.get("WEB_SECRET_KEY", "")
    web_listen_port: int = _int("WEB_LISTEN_PORT", 8000)
    web_allowed_networks: tuple = field(default_factory=lambda: _list("WEB_ALLOWED_NETWORKS"))

    # Claude connector (web/mcp.py): off unless a token is set. It reads
    # through its own login, which sees only the report views in MCP_VIEWS.
    # Totals by default; add "transactions" to let Claude see merchants.
    mcp_token: str = os.environ.get("MCP_TOKEN", "").strip()
    database_url_mcp: str = os.environ.get("DATABASE_URL_MCP", "")
    mcp_views: tuple = field(default_factory=lambda: _list("MCP_VIEWS") or (
        "accounts", "monthly", "balances", "consents", "imports", "household_monthly", "review",
        "category_monthly", "category_rules", "lending", "tax_estimate", "payslips",
        "expense_claims", "business_expenses", "vat_terms",
        "eurobonus", "points_monthly", "points_purchases", "trumf_purchases", "trumf_lines"))

    # Mail intake (mailin.py): off unless MAIL_USER is set. Attachments of
    # mail from MAIL_ALLOWED_SENDERS that passes Gmail's sender checks are
    # imported like uploads. The app password is a file, mounted read-only.
    mail_user: str = os.environ.get("MAIL_USER", "").strip()
    mail_password_file: str = os.environ.get("MAIL_PASSWORD_FILE", "/run/secrets/gmail-app-password")
    mail_imap_host: str = os.environ.get("MAIL_IMAP_HOST", "imap.gmail.com")
    mail_allowed_senders: tuple = field(default_factory=lambda: _list("MAIL_ALLOWED_SENDERS"))
    mail_poll_seconds: int = _int("MAIL_POLL_SECONDS", 300)
    mail_max_mb: int = _int("MAIL_MAX_MB", 15)

    # Alerts: Pushover, from a file holding the app token and user key
    # (notify.py), mounted read-only
    pushover_file: str = os.environ.get("PUSHOVER_FILE", "")

    @property
    def redirect_url(self) -> str:
        return f"{self.public_url}/eb/callback"


settings = Settings()
