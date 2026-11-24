-- Kari's consent fetched the shared accounts' history again. For one day
-- (15 October 2025) DNB sent eight payments without the payment reference
-- (KID) in front of the text and with new references, so they were stored
-- twice: "600000006 Giro, Tibber Norge As" and "Giro, Tibber Norge As".
--
-- 1. Remove such twins: same account, date and amount, one text being the
--    other without its leading reference number. A pair of genuine
--    payments both carry their own reference, so they are not touched.
-- 2. Recompute every fingerprint without the leading reference, as
--    heimkunnskap/normalise.py now does, so this cannot recur.

CREATE TEMP TABLE twin ON COMMIT DROP AS
SELECT DISTINCT ON (b.id) b.id
FROM txn a
JOIN txn b ON b.account_id = a.account_id
          AND b.booking_date = a.booking_date
          AND b.amount_minor = a.amount_minor
          AND b.id <> a.id
WHERE a.description ~ '^[0-9][0-9-]* +'
  AND b.description !~ '^[0-9]'
  AND regexp_replace(a.description, '^[0-9][0-9-]* +', '') = b.description;

DELETE FROM txn_class WHERE txn_id IN (SELECT id FROM twin) OR partner_txn_id IN (SELECT id FROM twin);
DELETE FROM txn WHERE id IN (SELECT id FROM twin);

-- Fingerprints: sha256 of "account_key|YYYY-MM-DD|amount_minor|text|n",
-- n counting identical rows per account, day, amount and text. Must match
-- add_fingerprints(); tests/test_fingerprint_sql.py checks that it does.
DROP INDEX txn_fp_uq;

WITH f AS (
    SELECT t.id,
           a.account_key || '|' || to_char(t.booking_date, 'YYYY-MM-DD') || '|' || t.amount_minor::text || '|'
             || regexp_replace(t.description, '^[0-9][0-9-]* +', '') AS base,
           row_number() OVER (
               PARTITION BY t.account_id, t.booking_date, t.amount_minor,
                            regexp_replace(t.description, '^[0-9][0-9-]* +', '')
               ORDER BY t.description COLLATE "C", coalesce(t.external_ref, '') COLLATE "C", t.id
           ) AS n
    FROM txn t
    JOIN account a ON a.id = t.account_id
)
UPDATE txn SET fingerprint = encode(sha256(convert_to(f.base || '|' || f.n, 'UTF8')), 'hex')
FROM f
WHERE f.id = txn.id;

CREATE UNIQUE INDEX txn_fp_uq ON txn (account_id, fingerprint) WHERE external_ref IS NULL;
