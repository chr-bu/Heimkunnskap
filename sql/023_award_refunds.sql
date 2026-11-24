-- Award bookings and their refunds (heimkunnskap/points.py, pair_awards).
--
-- A changed or cancelled award booking comes back as a positive posting
-- from "Scandinavian Airlines System" of exactly the booking's points. Each
-- such refund is paired with the booking it reverses, so both drop out and
-- what is left is the award travel actually kept.
--
-- flow: earned, award (points spent on flights, kept), award_changed (a
-- booking later refunded), award_refund, shop (EuroBonus Checkout, Shop),
-- spent (other points spent), level (level points only).

ALTER TABLE eb_txn ADD COLUMN flow text, ADD COLUMN refund_of bigint REFERENCES eb_txn (id);

CREATE OR REPLACE VIEW report.eurobonus AS
SELECT id, day, partner, kind, bonus_points, level_points, activity, flow, refund_of FROM eb_txn;
