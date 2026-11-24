-- The day of the last payment on a loan the household made. Payments up to
-- then that come after the principal is repaid are interest; without it the
-- loan ends when the principal is repaid. See pairing.lent().
ALTER TABLE lending ADD COLUMN until date;
