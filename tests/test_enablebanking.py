from datetime import datetime, timezone

import jwt
import pytest

from heimkunnskap.enablebanking import ApiError, EnableBanking, session_account_uids
from tests.fakes import FakeApi, key_pair

PRIVATE, PUBLIC = key_pair()


def make(routes):
    api = FakeApi(routes)
    return EnableBanking("app-123", PRIVATE, "https://eb.test", opener=api,
                         clock=lambda: 1_800_000_000), api


def test_token_is_rs256_with_kid_and_audience():
    eb, _ = make({})
    token = eb.token()
    assert jwt.get_unverified_header(token)["kid"] == "app-123"
    claims = jwt.decode(token, PUBLIC, algorithms=["RS256"], audience="api.enablebanking.com",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "enablebanking.com"
    assert claims["exp"] - claims["iat"] == 3600


def test_transactions_passes_only_given_params():
    eb, api = make({("GET", "/accounts/u1/transactions"):
                    lambda q, b: {"transactions": [], "continuation_key": None}})
    eb.transactions("u1", "2026-01-01", None, "longest")
    method, path, query, body, auth = api.calls[0]
    assert query == {"date_from": "2026-01-01", "strategy": "longest"}
    assert auth.startswith("Bearer ")


def test_start_auth_body():
    eb, api = make({("POST", "/auth"): lambda q, b: {"url": "https://bank.test/x"}})
    url = eb.start_auth("DNB", "NO", "https://heim.test/eb/callback", "s1",
                        datetime(2027, 5, 14, tzinfo=timezone.utc))
    assert url == "https://bank.test/x"
    body = api.calls[0][3]
    assert body["aspsp"] == {"name": "DNB", "country": "NO"}
    assert body["access"]["valid_until"] == "2027-05-14T00:00:00+00:00"
    assert body["state"] == "s1" and body["psu_type"] == "personal"


def test_errors_carry_status_and_classification():
    eb, _ = make({("GET", "/accounts/u1/balances"):
                  lambda q, b: (401, {"message": "Session expired"}),
                  ("GET", "/accounts/u2/balances"):
                  lambda q, b: (429, {"message": "Too many requests"})})
    with pytest.raises(ApiError) as e:
        eb.balances("u1")
    assert e.value.status == 401 and e.value.consent_gone
    with pytest.raises(ApiError) as e:
        eb.balances("u2")
    assert e.value.rate_limited and not e.value.consent_gone


def test_session_account_uids_accepts_both_shapes():
    assert session_account_uids({"accounts": ["a", {"uid": "b"}, {"x": 1}]}) == ["a", "b"]
    assert session_account_uids({}) == []
