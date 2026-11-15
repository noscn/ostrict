CREATE SEMANTIC VIEW ecommerce
  TABLES (
    customers AS shop.customers PRIMARY KEY (customer_id)
      COMMENT = 'One row per customer. Primary date: signup_date. Source: shop.customers.',
    orders AS shop.orders PRIMARY KEY (order_id)
      COMMENT = 'One row per order. Primary date: order_date. Partition: order_date BY order_date. Source: shop.orders.',
    order_items AS shop.order_items PRIMARY KEY (order_id, line_no)
      COMMENT = 'One row per order line. Primary date: order_date. Partition: order_date BY order_date. Source: shop.order_items.',
    products AS shop.products PRIMARY KEY (product_id)
      COMMENT = 'One row per product. Primary date: created_at. Source: shop.products.',
    daily_inventory_snapshot AS shop.daily_inventory_snapshot PRIMARY KEY (snapshot_date, product_id)
      COMMENT = 'One row per product per day. Point-in-time snapshot. Primary date: snapshot_date. Partition: snapshot_date BY snapshot_date. Source: shop.daily_inventory_snapshot.'
  )
  RELATIONSHIPS (
    orders_customers AS orders (customer_id) REFERENCES customers (customer_id),
    items_orders AS order_items (order_id) REFERENCES orders (order_id),
    items_products AS order_items (product_id) REFERENCES products (product_id),
    inventory_products AS daily_inventory_snapshot (product_id) REFERENCES products (product_id)
  )
  FACTS (
    order_items.line_revenue AS quantity * unit_price
      COMMENT = 'Row-level revenue for one order line: quantity times unit price.'
  )
  DIMENSIONS (
    customers.region AS region WITH SYNONYMS ('market', 'country_group')
      COMMENT = 'Customer home region. Values: EU, US, APAC.',
    customers.segment AS CASE WHEN is_vip THEN 'vip' ELSE 'regular' END
      COMMENT = 'Loyalty segment derived from the vip flag. Values: vip, regular.',
    customers.signup_date AS signup_date
      COMMENT = 'Date the customer signed up. Grains: day, month.',
    orders.order_date AS order_date
      COMMENT = 'Date the order was placed. Grains: day, week, month.',
    orders.channel AS channel
      COMMENT = 'Sales channel the order came through. Values: web, app, store.',
    orders.order_status AS status
      COMMENT = 'Lifecycle status of the order. Values: completed, cancelled.',
    products.category AS category
      COMMENT = 'Product merchandising category. Values: electronics, books.',
    daily_inventory_snapshot.snapshot_date AS snapshot_date
      COMMENT = 'Inventory snapshot date. Grains: day.'
  )
  METRICS (
    order_items.revenue AS SUM(line_revenue)
      COMMENT = 'Total revenue across order lines. Population: all order lines. Additivity: additive.',
    order_items.units_sold AS SUM(quantity)
      COMMENT = 'Units sold across order lines. Population: all order lines. Additivity: additive.',
    orders.order_count AS COUNT(order_id)
      COMMENT = 'Number of orders placed. Population: all orders. Additivity: additive.',
    orders.buyer_count AS COUNT(DISTINCT customer_id)
      COMMENT = 'Distinct customers who placed an order. Population: all orders. Additivity: non-additive.',
    customers.customer_count AS COUNT(customer_id)
      COMMENT = 'Number of customers on file. Population: all customers. Additivity: additive.',
    daily_inventory_snapshot.stock_units AS SUM(units_on_hand) NON ADDITIVE BY (snapshot_date)
      COMMENT = 'Units on hand at the snapshot date. Population: all products. Additivity: non-additive.',
    aov AS revenue / order_count
      COMMENT = 'Average order value: revenue divided by order count. Additivity: ratio.'
  )
  COMMENT = 'Retail e-commerce sales and inventory for the demo warehouse.'
  AI_SQL_GENERATION 'Route revenue, order and inventory questions here. Always filter order_date or snapshot_date.'
