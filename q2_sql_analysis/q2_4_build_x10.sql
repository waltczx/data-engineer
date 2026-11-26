-- ============================================================
-- 生成 10 倍数据量的测试库 olist_x10，用于验证 Q2.4 慢查询在数据增长后的表现。
-- 只复制慢查询涉及的 4 张表。每条记录复制 10 份，ID 用 MD5(原ID-序号) 生成，
-- 保持 32 位十六进制格式，表间关联关系不变。需要建库权限；约 6 分钟。
-- 用法：mysql -uroot -p < q2_sql_analysis/q2_4_build_x10.sql
-- ============================================================
DROP DATABASE IF EXISTS olist_x10;
CREATE DATABASE olist_x10 DEFAULT CHARACTER SET utf8mb4;
USE olist_x10;

CREATE TABLE n (k INT PRIMARY KEY);
INSERT INTO n VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9);

-- CREATE TABLE ... LIKE 会连同主键和二级索引一起复制
CREATE TABLE customers     LIKE olist.customers;
CREATE TABLE orders        LIKE olist.orders;
CREATE TABLE order_items   LIKE olist.order_items;
CREATE TABLE order_reviews LIKE olist.order_reviews;

INSERT INTO customers
SELECT MD5(CONCAT(c.customer_id, '-', n.k)), c.customer_unique_id, c.customer_zip_code_prefix,
       c.customer_city, c.customer_state
FROM olist.customers AS c CROSS JOIN n;

INSERT INTO orders
SELECT MD5(CONCAT(o.order_id, '-', n.k)), MD5(CONCAT(o.customer_id, '-', n.k)), o.order_status,
       o.order_purchase_timestamp, o.order_approved_at, o.order_delivered_carrier_date,
       o.order_delivered_customer_date, o.order_estimated_delivery_date
FROM olist.orders AS o CROSS JOIN n;

INSERT INTO order_items
SELECT MD5(CONCAT(i.order_id, '-', n.k)), i.order_item_id, i.product_id, i.seller_id,
       i.shipping_limit_date, i.price, i.freight_value
FROM olist.order_items AS i CROSS JOIN n;

INSERT INTO order_reviews
SELECT MD5(CONCAT(r.review_id, '-', n.k)), MD5(CONCAT(r.order_id, '-', n.k)), r.review_score,
       r.review_comment_title, r.review_comment_message, r.review_creation_date, r.review_answer_timestamp
FROM olist.order_reviews AS r CROSS JOIN n;

DROP TABLE n;
