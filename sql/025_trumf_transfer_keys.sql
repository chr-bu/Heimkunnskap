-- Transfers to EuroBonus come from Trumf without a batchId, so all of them
-- were stored as one row with the key ''. They now get a key of their own
-- (heimkunnskap/trumf.py, keys()); the next fetch stores them again.
DELETE FROM trumf_txn WHERE batch_id = '';
