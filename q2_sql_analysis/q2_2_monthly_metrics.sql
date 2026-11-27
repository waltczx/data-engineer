-- ============================================================
-- Q2.2 平台真实成交分析：按月统计（MySQL 8.0）
--
-- 口径：
-- * month：order_purchase_timestamp 所在月份（YYYY-MM）。
-- * total_orders：当月所有状态的订单数。
-- * delivered_orders：状态为 'delivered' 且实际送达时间不为空的订单数。
-- * canceled_orders：状态为 'canceled' 的订单数。
--   'unavailable'（609 单，多数没有商品明细）不计入取消，题目把取消定义为 canceled 状态。
-- * cancel_rate：canceled_orders / total_orders，单位为百分比，保留 2 位小数（例：0.63 表示 0.63%）。
--   题目只说"保留 2 位小数"。如果用 0~1 的比例，绝大多数月份会显示成 0.00 或 0.01，看不出差别，所以用百分比。
-- * delivered_gmv：已送达订单的 SUM(price + COALESCE(freight_value, 0))。
-- * avg_delivery_days：已送达订单从下单到送达的平均天数，按秒差 / 86400 计，保留 2 位小数。
--
-- 写法说明：
-- 1. 只为已送达且送达时间非空的订单汇总 order_items，再和 orders 关联。
--    直接 orders JOIN order_items 会让多件商品的订单出现多行，COUNT(*) 会把订单数算大。
-- 2. 用 LEFT JOIN：775 个订单没有任何商品明细（大多是 unavailable、canceled），
--    内连接会让它们从总下单量和取消率的分母里消失。
-- 3. 送达时间为空的处理：有 8 个订单状态是 delivered 但送达时间为空。
--    因无法确认实际送达，这些订单不计入 delivered_orders、delivered_gmv 或平均送达天数。
--    另有 6 个 canceled 订单有送达时间，按状态判断，不会被算成已送达。
-- 4. 运费为空时按 0 计算，保留该商品的 price 参与 GMV 汇总。
-- ============================================================
WITH order_value AS (
    SELECT
        oi.order_id,
        SUM(oi.price + COALESCE(oi.freight_value, 0)) AS order_gmv
    FROM orders AS delivered
    JOIN order_items AS oi ON oi.order_id = delivered.order_id
    WHERE delivered.order_status = 'delivered'
      AND delivered.order_delivered_customer_date IS NOT NULL
    GROUP BY oi.order_id
),
order_base AS (
    SELECT
        DATE_FORMAT(o.order_purchase_timestamp, '%Y-%m') AS month,
        o.order_status,
        (o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL) AS is_delivered,
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
    SUM(is_delivered)                                                AS delivered_orders,
    SUM(order_status = 'canceled')                                   AS canceled_orders,
    ROUND(100 * SUM(order_status = 'canceled') / COUNT(*), 2)        AS cancel_rate,
    ROUND(COALESCE(SUM(CASE WHEN is_delivered THEN order_gmv END), 0), 2) AS delivered_gmv,
    ROUND(AVG(delivery_days), 2)                                     AS avg_delivery_days
FROM order_base
GROUP BY month
ORDER BY month;
