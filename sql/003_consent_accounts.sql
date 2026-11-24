-- Several BankID consents per bank: one per person. Ola's consent
-- reaches the accounts he holds or may use; Kari's reaches her own savings
-- as well. An account can sit under both, so which consent covers which
-- account is a table of its own, with the uid that session gave it.
--
-- A new consent replaces an older one for the same bank only when it
-- covers every account the older one did (heimkunnskap/db.py). Each account
-- is synced once a night, under the active consent that lasts longest.

ALTER TABLE consent ADD COLUMN label text;    -- whose BankID, e.g. "Kari"

CREATE TABLE consent_account (
    consent_id  bigint NOT NULL REFERENCES consent (id),
    account_id  bigint NOT NULL REFERENCES account (id),
    uid         text NOT NULL,                -- Enable Banking uid in that session
    PRIMARY KEY (consent_id, account_id)
);

INSERT INTO consent_account (consent_id, account_id, uid)
SELECT consent_id, id, current_uid
FROM account
WHERE consent_id IS NOT NULL AND current_uid IS NOT NULL;

-- New columns go last: CREATE OR REPLACE VIEW keeps the existing ones and
-- their grants.
CREATE OR REPLACE VIEW report.consents AS
SELECT c.id,
       c.aspsp_name AS bank,
       c.status,
       c.valid_until,
       greatest(0, ceil(extract(epoch FROM c.valid_until - now()) / 86400))::int AS days_left,
       c.last_sync_at,
       c.last_error,
       c.label,
       (SELECT count(*) FROM consent_account ca WHERE ca.consent_id = c.id) AS accounts
FROM consent c;
