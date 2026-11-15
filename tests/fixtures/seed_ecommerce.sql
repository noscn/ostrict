CREATE SCHEMA shop;

CREATE TABLE shop.customers (customer_id INT, region TEXT, is_vip BOOLEAN, signup_date DATE);
INSERT INTO shop.customers VALUES
  (1, 'EU', true,  DATE '2025-11-03'),
  (2, 'US', false, DATE '2025-12-14'),
  (3, 'EU', false, DATE '2026-01-02'),
  (4, 'APAC', false, DATE '2026-01-15');

CREATE TABLE shop.orders (order_id INT, customer_id INT, order_date DATE, channel TEXT, status TEXT);
INSERT INTO shop.orders VALUES
  (1, 1,  DATE '2026-01-05', 'web',   'completed'),
  (2, 1,  DATE '2026-02-10', 'app',   'completed'),
  (3, 2,  DATE '2026-01-20', 'web',   'completed'),
  (4, 3,  DATE '2026-01-07', 'store', 'completed'),
  (5, 99, DATE '2026-01-09', 'web',   'completed'),   -- orphan: customer 99 does not exist
  (6, 2,  DATE '2026-01-25', 'web',   'cancelled');

CREATE TABLE shop.order_items
  (order_id INT, line_no INT, product_id INT, quantity INT, unit_price DECIMAL(10, 2), order_date DATE);
INSERT INTO shop.order_items VALUES
  (1, 1, 1, 2, 5.00,   DATE '2026-01-05'),
  (1, 2, 2, 1, 10.00,  DATE '2026-01-05'),
  (2, 1, 1, 1, 100.00, DATE '2026-02-10'),
  (3, 1, 2, 3, 1.00,   DATE '2026-01-20'),
  (3, 2, 2, 1, 1.00,   DATE '2026-01-20'),
  (3, 3, 1, 2, 2.00,   DATE '2026-01-20'),
  (4, 1, 1, 1, 50.00,  DATE '2026-01-07'),
  (5, 1, 2, 1, 7.00,   DATE '2026-01-09'),
  (6, 1, 1, 1, 9.00,   DATE '2026-01-25');

CREATE TABLE shop.products (product_id INT, category TEXT, created_at DATE);
INSERT INTO shop.products VALUES
  (1, 'electronics', DATE '2025-10-01'),
  (2, 'books',       DATE '2025-10-02');

CREATE TABLE shop.daily_inventory_snapshot (snapshot_date DATE, product_id INT, units_on_hand INT);
INSERT INTO shop.daily_inventory_snapshot VALUES
  (DATE '2026-01-01', 1, 10),
  (DATE '2026-01-01', 2, 20),
  (DATE '2026-01-02', 1, 30),
  (DATE '2026-01-02', 2, 40);
