-- Electricity support from the grid company (Elvia) lowers the electricity
-- cost: heimkunnskap/pairing.py marks it as a reduction of spending, and
-- this rule puts it in the same category as the electricity bills.
INSERT INTO category_rule (category_id, pattern, priority)
SELECT id, '\belvia\b', 55 FROM category WHERE name = 'Home, utilities and cabin';
