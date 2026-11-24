-- A hand mark that says a payment is ordinary income or spending, whatever
-- the rules say: `python -m heimkunnskap.manual mark ID ordinary`. For
-- instance a monthly fund purchase the bank profile takes for saving, when
-- the fund belongs to someone outside the household: the money is given
-- away, so it is spending, sorted into a category by the category rules.
--
-- Stored as a manual txn_class row without a kind. The reports treat a
-- missing kind as ordinary income and spending already (they join
-- txn_class and test kind IS NULL); the manual row keeps the pairing rules
-- from classifying the payment again. Only a hand mark may lack a kind.
ALTER TABLE txn_class ALTER COLUMN kind DROP NOT NULL;
ALTER TABLE txn_class ADD CONSTRAINT txn_class_kind_or_manual
    CHECK (kind IS NOT NULL OR manual);
