-- ============================================================
-- Q2.2 平台真实成交分析：按月统计（MySQL 8.0）
--
-- 口径：
-- * month：order_purchase_timestamp 所在月份（YYYY-MM）。
-- * total_orders：当月所有状态的订单数。
-- * delivered_orders / canceled_orders：order_status 分别为 'delivered' / 'canceled' 的订单数。
--   'unavailable'（609 单，多数没有商品明细）不计入取消，题目把取消定义为 canceled 状态。
-- * cancel_rate：canceled_orders / total_orders，单位为百分比，保留 2 位小数（例：0.63 表示 0.63%）。
--   题目只说"保留 2 位小数"。如果用 0~1 的比例，绝大多数月份会显示成 0.00 或 0.01，看不出差别，所以用百分比。
-- * delivered_gmv：已送达订单的 SUM(price + freight_value)。
-- * avg_delivery_days：已送达订单从下单到送达的平均天数，按秒差 / 86400 计，保留 2 位小数。
--
-- 写法说明：
-- 1. 先在 CTE 里把 order_items 按订单汇总成一行，再和 orders 关联。
--    直接 orders JOIN order_items 会让多件商品的订单出现多行，COUNT(*) 会把订单数算大。
-- 2. 用 LEFT JOIN：775 个订单没有任何商品明细（大多是 unavailable、canceled），
--    内连接会让它们从订单数里消失，取消率就算错了。
-- 3. 送达时间为空的处理：有 8 个订单状态是 delivered 但送达时间为空。
--    它们计入 delivered_orders 和 delivered_gmv（确实已送达并成交），
--    但不参与平均送达天数（CASE 返回 NULL，AVG 自动忽略 NULL）。
--    另有 6 个 canceled 订单有送达时间，按状态判断，不会被算成已送达。
-- ============================================================
WITH order_value AS (
    SELECT
        order_id,
        SUM(price + freight_value) AS order_gmv
    FROM order_items
    GROUP BY order_id
),
order_base AS (
    SELECT
        DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month,
        o.order_status,
        v.order_gmv,
        CASE
            WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
            THEN TIMESTAMPDIFF(SECOND, o.order_purchase_timestamp, o.order_delivered_customer_date) / 86400
        END AS delivery_days
    FROM orders AS o
    LEFT JOIN order_value AS v ON v.order_id = o.order_id
)
SELECT
    month,
    COUNT(*)                                                         AS total_orders,
    SUM(order_status = 'delivered')                                  AS delivered_orders,
    SUM(order_status = 'canceled')                                   AS canceled_orders,
    ROUND(100 * SUM(order_status = 'canceled') / COUNT(*), 2)        AS cancel_rate,
    ROUND(COALESCE(SUM(CASE WHEN order_status = 'delivered' THEN order_gmv END), 0), 2) AS delivered_gmv,
    ROUND(AVG(delivery_days), 2)                                     AS avg_delivery_days
FROM order_base
GROUP BY month
ORDER BY month;
