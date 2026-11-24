-- Income year 2025, for checking the estimate against an assessment already
-- made (skatteoppgjør 2025). Verified: the rates reproduce a real 2025
-- assessment to the krone (trinnskatt and trygdeavgift exactly; the
-- general income tax to within a krone).
-- Amounts in kroner.

INSERT INTO tax_rule (year, params, source) VALUES (2025, '{
  "general_rate": 0.22,
  "personal_allowance": 108550,
  "minimum_deduction_rate": 0.46,
  "minimum_deduction_max": 92000,
  "brackets": [[217400, 0.017], [306050, 0.040], [697150, 0.137], [942400, 0.167], [1410750, 0.177]],
  "social_security_salary": 0.077,
  "social_security_business": 0.109,
  "social_security_floor": 99650,
  "dividend_factor": 1.72
}', 'smartepenger.no 657 skattesatser 2025; checked against a skatteoppgjør 2025');
