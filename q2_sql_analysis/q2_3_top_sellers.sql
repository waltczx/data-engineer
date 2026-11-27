-- ============================================================
-- Q2.3 卖家维度分析：2017 年 GMV TOP 20 卖家（MySQL 8.0）
--
-- 口径（题目未完全定义的地方，按以下假设处理）：
-- * 2017 年：order_purchase_timestamp 在 2017-01-01（含）到 2018-01-01（不含）之间。
-- * gmv_2017：该卖家在 2017 年状态为 delivered 且送达时间非空的订单中，
--   汇总 price + COALESCE(freight_value, 0)，与 Q2.2 的"真实成交"口径一致。
--   一个订单可能包含多个卖家的商品（1,278 个订单），每个卖家只算自己那部分商品。
-- * order_count：该卖家 2017 年涉及的订单数（所有状态），也是取消率的分母。
-- * unique_customers：不重复的 customer_unique_id。
--   不能用 customer_id：Olist 给每个订单生成一个新的 customer_id（99,441 个），
--   同一个人在 customer_unique_id 上只有一个值（96,096 个），用 customer_id 会把回头客算成多个人。
-- * avg_review_score：每个订单先去重为一条评价，再对卖家的订单求平均。
--   有 547 个订单有 2~3 条评价，其中 202 个订单的几条评分不同。
--   去重规则：保留最晚回复（review_answer_timestamp 最大）的那条，视为客户的最终评价。
--   如果不去重，多评价订单的分数会被重复计入；而且下面与卖家订单关联时还会让 GMV 翻倍。
-- * late_delivery_rate：实际与预计送达时间都不为空的订单中，送达日期 > 预计送达日期的比例，不按订单状态筛选。
--   预计送达时间都是 00:00:00，只有日期有意义，所以按日期比较：当天送达不算延迟。
--   状态不是 delivered 但有实际送达时间的订单也参与计算。
-- * cancel_rate：order_status = 'canceled' 的订单 / order_count。
-- * 两个比率均用百分比，保留 2 位小数。
-- * state_rank：在卖家所在州内，按 gmv_2017 排名（RANK，并列同名次）。
--   先在全部卖家中排名，再取全国 TOP 20；如果先取 TOP 20 再排名，州内名次会只在这 20 人里算，是错的。
-- ============================================================
WITH seller_orders AS (
    -- 一行 = 一个卖家在一个订单里的部分；先按 (卖家, 订单) 聚合，避免后续关联时重复计算
    SELECT
        oi.seller_id,
        o.order_id,
        o.order_status,
        c.customer_unique_id,
        o.order_delivered_customer_date,
        o.order_estimated_delivery_date,
        SUM(CASE
            WHEN o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
            THEN oi.price + COALESCE(oi.freight_value, 0)
            ELSE 0
        END) AS seller_order_gmv
    FROM orders AS o
    JOIN order_items AS oi ON oi.order_id = o.order_id
    JOIN customers   AS c  ON c.customer_id = o.customer_id
    WHERE o.order_purchase_timestamp >= '2017-01-01'
      AND o.order_purchase_timestamp <  '2018-01-01'
    GROUP BY
        oi.seller_id, o.order_id, o.order_status, c.customer_unique_id,
        o.order_delivered_customer_date, o.order_estimated_delivery_date
),
ranked_reviews AS (
    SELECT
        order_id,
        review_score,
        ROW_NUMBER() OVER (
            PARTITION BY order_id
            ORDER BY review_answer_timestamp DESC, review_creation_date DESC, review_id
        ) AS rn
    FROM order_reviews
),
order_review AS (
    -- 每个订单只保留一条评价
    SELECT order_id, review_score
    FROM ranked_reviews
    WHERE rn = 1
),
seller_stats AS (
    SELECT
        so.seller_id,
        SUM(so.seller_order_gmv) AS gmv_2017,
        COUNT(*)                                     AS order_count,
        COUNT(DISTINCT so.customer_unique_id)        AS unique_customers,
        AVG(r.review_score)                          AS avg_review_score,
        SUM(CASE
            WHEN so.order_delivered_customer_date IS NOT NULL
             AND so.order_estimated_delivery_date IS NOT NULL
             AND DATE(so.order_delivered_customer_date) > DATE(so.order_estimated_delivery_date)
            THEN 1 ELSE 0
        END) / NULLIF(SUM(CASE
            WHEN so.order_delivered_customer_date IS NOT NULL
             AND so.order_estimated_delivery_date IS NOT NULL
            THEN 1 ELSE 0
        END), 0) AS late_delivery_rate,
        SUM(so.order_status = 'canceled') / COUNT(*) AS cancel_rate
    FROM seller_orders AS so
    LEFT JOIN order_review AS r ON r.order_id = so.order_id
    GROUP BY so.seller_id
),
ranked AS (
    SELECT
        st.*,
        s.seller_state,
        RANK()       OVER (PARTITION BY s.seller_state ORDER BY st.gmv_2017 DESC) AS state_rank,
        ROW_NUMBER() OVER (ORDER BY st.gmv_2017 DESC, st.seller_id)               AS overall_rank
    FROM seller_stats AS st
    JOIN sellers      AS s ON s.seller_id = st.seller_id
)
SELECT
    seller_id,
    seller_state,
    ROUND(gmv_2017, 2)                 AS gmv_2017,
    order_count,
    unique_customers,
    ROUND(avg_review_score, 2)         AS avg_review_score,
    ROUND(100 * late_delivery_rate, 2) AS late_delivery_rate,
    ROUND(100 * cancel_rate, 2)        AS cancel_rate,
    state_rank
FROM ranked
WHERE overall_rank <= 20
ORDER BY overall_rank;
