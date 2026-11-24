-- Pets of any kind, not only dogs. Renamed in place, so rules and history
-- follow.
UPDATE category SET name = 'Pets' WHERE name = 'Dog'
  AND NOT EXISTS (SELECT 1 FROM category WHERE name = 'Pets');
