from heimkunnskap.categories import categorise, check_pattern, compile_rules

RULES = compile_rules([
    {"id": 1, "category_id": 10, "pattern": r"\bkiwi\b|\bmeny\b", "priority": 20},
    {"id": 2, "category_id": 20, "pattern": r"\bhotel|\btokyo\b", "priority": 40},
    {"id": 3, "category_id": 30, "pattern": r"medlemsavgift", "priority": 10},
    {"id": 4, "category_id": 40, "pattern": r"(broken", "priority": 5},
])
OTHER = 99


def t(i, text, party=None):
    return {"id": i, "counterparty": party, "description": text}


def test_first_match_by_priority_and_other():
    out = categorise([t(1, "Kiwi 100 Eksempelveien Oslo"), t(2, "HOTEL EKSEMPEL TOKYO"),
                      t(3, "MEDLEMSAVGIFT"), t(4, "Varekjøp", party="Meny Eksempel"),
                      t(5, "Kiwiana Imports")], RULES, OTHER)
    assert out == {1: (10, 1), 2: (20, 2), 3: (30, 3), 4: (10, 1), 5: (OTHER, None)}


def test_a_reimbursement_follows_its_expense():
    rows = [t(1, "HOTEL EKSEMPEL TOKYO"), t(2, "utlegg ABCD"), t(3, "utlegg uten match")]
    out = categorise(rows, RULES, OTHER, reimbursed={2: 1, 3: None})
    assert out[2] == (20, None) and out[3] == (OTHER, None)


def test_check_pattern():
    assert check_pattern(r"\bkiwi\b") is None
    assert "not a valid pattern" in check_pattern("(kiwi")
    assert check_pattern("") and check_pattern("x" * 301)
    assert check_pattern(".*") == "the pattern matches everything"


def test_cost_reduction_without_expense_uses_its_own_text():
    rules = compile_rules([{"id": 5, "category_id": 50, "pattern": r"\belvia\b", "priority": 55}])
    out = categorise([t(1, "Overføring Innland, Elvia As")], rules, OTHER, reimbursed={1: None})
    assert out[1] == (50, 5)
