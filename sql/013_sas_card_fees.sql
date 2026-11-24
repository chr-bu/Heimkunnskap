-- Card fees as the SAS EuroBonus Mastercard names them, in its bank feed
-- (Swedish) and in its Excel export (Norwegian): the yearly fee, a
-- replacement card, and the fee for cash from an ATM.
INSERT INTO category_rule (category_id, pattern, priority)
SELECT id, 'kortkontingent|ers[äa]ttningskort|erstatningskort|kontantuttagsavgift|avgift uttak minibank', 110
FROM category WHERE name = 'Card and bank fees';
