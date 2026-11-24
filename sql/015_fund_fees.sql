-- The fund platform's quarterly fee ("Plattformhonorar Fond") is a bank fee
INSERT INTO category_rule (category_id, pattern, priority)
SELECT id, 'plattformhonorar', 110 FROM category WHERE name = 'Card and bank fees';
