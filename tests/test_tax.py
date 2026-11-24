from datetime import date

import pytest

from heimkunnskap.tax import Inputs, bracket_tax, compute, monthly_interest_projection, project_slip

RULES_2026 = {
    "general_rate": 0.22, "personal_allowance": 114540,
    "minimum_deduction_rate": 0.46, "minimum_deduction_max": 95700,
    "brackets": [[226100, 0.017], [318300, 0.040], [725050, 0.137], [980100, 0.168], [1467200, 0.178]],
    "social_security_salary": 0.076, "social_security_business": 0.108,
    "social_security_floor": 99650, "dividend_factor": 1.72,
}

RULES_2025 = {
    "general_rate": 0.22, "personal_allowance": 108550,
    "minimum_deduction_rate": 0.46, "minimum_deduction_max": 92000,
    "brackets": [[217400, 0.017], [306050, 0.040], [697150, 0.137], [942400, 0.167], [1410750, 0.177]],
    "social_security_salary": 0.077, "social_security_business": 0.109,
    "social_security_floor": 99650, "dividend_factor": 1.72,
}


def test_2025_rules_by_hand():
    """Synthetic figures worked out by hand with the 2025 rates, which also
    reproduce a real 2025 assessment to the krone."""
    r = compute(Inputs("A", salary=150_000_000, pension=2_000_000, interest_paid=15_000_000,
                       other_deduction=500_000), RULES_2025)
    assert r["minimum_deduction"] == 92000
    assert r["general_income"] == 1_500_000 - 92_000 - 20_000 - 150_000 - 5_000
    assert r["general_tax"] == pytest.approx((1_233_000 - 108_550) * 0.22)
    # 88,650 × 1.7% + 391,100 × 4% + 245,250 × 13.7% + 468,350 × 16.7% + 89,250 × 17.7%
    assert r["bracket_tax"] == pytest.approx(1507.05 + 15644 + 33599.25 + 78214.45 + 15797.25)
    assert r["social_security"] == pytest.approx(115500)
    assert r["total_tax"] == pytest.approx(507641)


def test_salary_only_by_hand():
    r = compute(Inputs("A", salary=100_000_000), RULES_2026)          # 1,000,000 kr
    assert r["minimum_deduction"] == 95700
    assert r["general_tax"] == pytest.approx((1_000_000 - 95700 - 114540) * 0.22)
    # 92,200 × 1.7% + 406,750 × 4% + 255,050 × 13.7% + 19,900 × 16.8%
    assert r["bracket_tax"] == pytest.approx(1567.4 + 16270 + 34941.85 + 3343.2)
    assert r["social_security"] == pytest.approx(76000)
    assert r["total_tax"] == pytest.approx(173747.2 + 56122.45 + 76000)
    assert r["marginal_rate_salary"] == pytest.approx(0.22 + 0.076 + 0.168)


def test_business_profit_stacks_on_salary():
    base = compute(Inputs("A", salary=150_000_000), RULES_2026)
    more = compute(Inputs("A", salary=150_000_000, business_profit=10_000_000), RULES_2026)
    # 100,000 kr more profit, all in the top bracket: 22% + 10.8% + 17.8%
    assert more["total_tax"] - base["total_tax"] == pytest.approx(100000 * (0.22 + 0.108 + 0.178))
    assert more["marginal_rate_business"] == pytest.approx(0.506)


def test_deductions_and_balance():
    r = compute(Inputs("A", salary=80_000_000, pension=2_000_000, interest_paid=30_000_000,
                       dividends=1_000_000, withheld=25_000_000), RULES_2026)
    general = 800000 - 95700 - 20000 - 300000 + 10000 * 1.72
    assert r["general_income"] == pytest.approx(general)
    assert r["general_tax"] == pytest.approx((general - 114540) * 0.22)
    assert r["balance"] == pytest.approx(250000 - r["total_tax"])


def test_low_income_social_security_floor():
    assert compute(Inputs("A", salary=9_000_000), RULES_2026)["social_security"] == 0
    r = compute(Inputs("A", salary=11_000_000), RULES_2026)
    assert r["social_security"] == pytest.approx(min(110000 * 0.076, (110000 - 99650) * 0.25))


def test_bracket_tax_below_the_first_step():
    assert bracket_tax(200000, RULES_2026["brackets"]) == 0


def test_project_slip_rest_of_year_with_half_tax_in_december():
    slip = {"period": date(2026, 10, 1), "employer": "Eksempel AS", "gross_ytd_minor": 80_000_000,
            "salary_minor": 9_000_000, "benefits_minor": 100_000, "pension_ytd_minor": 0,
            "pension_minor": 0, "withheld_ytd_minor": 28_000_000, "withholding_minor": 3_000_000}
    salary, pension, withheld, notes = project_slip(slip, 2026)
    assert salary == 80_000_000 + 2 * 9_100_000
    assert withheld == 28_000_000 + 3_000_000 + 1_500_000        # November full, December half
    assert "2 more months" in notes[0]
    december = {**slip, "period": date(2026, 12, 1)}
    assert project_slip(december, 2026)[2] == 28_000_000


def test_interest_projection_uses_the_last_three_months():
    paid = {m: 2_000_000 for m in range(1, 11)} | {9: 2_100_000, 10: 2_200_000, 11: 500_000}
    total, note = monthly_interest_projection(paid, date(2026, 11, 17), 2026)
    avg = round((2_000_000 + 2_100_000 + 2_200_000) / 3)
    assert total == 2_000_000 * 8 + 2_100_000 + 2_200_000 + avg * 2
    past, _ = monthly_interest_projection(paid, date(2027, 2, 1), 2026)
    assert past == sum(paid.values())


def test_decimal_inputs_from_the_database_driver():
    """asyncpg returns sum(bigint) as Decimal; mixing it with floats raised TypeError
    on the real database (the test shim returns ints)."""
    from decimal import Decimal
    total, _ = monthly_interest_projection({m: Decimal(2_558_500) for m in range(1, 11)},
                                           date(2026, 11, 17), 2026)
    r = compute(Inputs("A", salary=Decimal(150_000_000), interest_paid=total,
                       other_deduction=Decimal(0), withheld=Decimal(50_000_000)), RULES_2026)
    assert r["interest_deduction"] == pytest.approx(25585 * 12)
    import json
    json.dumps(r)                                    # the stored estimate must serialise


def test_half_tax_taken_in_november():
    october = {"period": date(2026, 10, 1), "employer": "Eksempel AS", "gross_ytd_minor": 88_000_000,
               "salary_minor": 8_923_650, "benefits_minor": 91_400, "pension_ytd_minor": 0,
               "pension_minor": 0, "withheld_ytd_minor": 28_527_100, "withholding_minor": 3_325_400}
    november = {**october, "period": date(2026, 11, 1), "gross_ytd_minor": 97_100_000,
                "withheld_ytd_minor": 30_222_600, "withholding_minor": 1_662_700}
    salary, _, withheld, notes = project_slip(november, 2026, october)
    assert withheld == 30_222_600 + 3_325_400                    # December full
    assert "half tax taken in 11/2026" in notes[0]
    assert project_slip(november, 2026)[2] == 30_222_600 + 1_662_700 - 831_350   # no slip before: guess
    # A normal November after October: December half, as before
    normal = {**november, "withholding_minor": 3_325_400, "withheld_ytd_minor": 31_852_500}
    assert project_slip(normal, 2026, october)[2] == 31_852_500 + 1_662_700
    # Lower pay that month (unpaid leave): not taken for half tax
    leave = {**november, "salary_minor": 4_000_000}
    assert "half tax" not in project_slip(leave, 2026, october)[3][0]
