-- ============================================================
-- Q2.1 商品类别翻译完整性（MySQL 8.0）
-- 找出 products 中存在、但翻译表里没有英文对照的类别，并统计每个类别的商品数。
--
-- 写法说明：
-- 1. 用 LEFT JOIN ... WHERE t.product_category_name IS NULL（反连接），
--    而不是 NOT IN (SELECT ...)：子查询结果里只要有一个 NULL，NOT IN 就会整体返回空。
-- 2. products 中有 610 个商品的类别本身是 NULL。NULL 和任何值都 JOIN 不上，
--    所以它也会出现在结果里。它不是"翻译缺失"，而是"类别缺失"，单独标成 (类别为空)。
--    这样结果是 3 行：2 个真正没有翻译的类别 + 1 个空类别。
-- ============================================================

WITH category_counts AS (
    SELECT
        NULLIF(TRIM(product_category_name), '') AS category_name,
        COUNT(*) AS product_count
    FROM products
    GROUP BY NULLIF(TRIM(product_category_name), '')
)
SELECT
    COALESCE(c.category_name, '(类别为空)') AS product_category_name,
    CASE
        WHEN c.category_name IS NULL THEN '类别缺失'
        ELSE '翻译缺失'
    END AS gap_type,
    c.product_count
FROM category_counts AS c
LEFT JOIN product_category_name_translation AS t
    ON t.product_category_name = c.category_name
WHERE c.category_name IS NULL
   OR NULLIF(TRIM(t.product_category_name_english), '') IS NULL
ORDER BY c.product_count DESC, product_category_name;

-- ============================================================
-- 进阶：缺失翻译的 3 种处理方案
--
-- 方案 1：人工补全翻译表（推荐用于 2 个缺失类别）
--   INSERT INTO product_category_name_translation VALUES
--     ('pc_gamer', 'pc_gamer'),
--     ('portateis_cozinha_e_preparadores_de_alimentos', 'portable_kitchen_and_food_preparers');
--   利：一次修好，所有下游报表一致；只有 2 个类别，成本很低。
--   弊：需要人维护；新类别出现时会再次缺失，要配合下面的监控。
--
-- 方案 2：查询时兜底，COALESCE(t.product_category_name_english, p.product_category_name)
--   利：不改数据，报表不会出现空白；新类别出现时自动有显示值。
--   弊：报表里中混入葡萄牙语原名，按英文名分组或排序时不一致；问题被掩盖，没人会去补翻译。
--
-- 方案 3：统一归入 'unknown' / 'other'，并在 ETL 中加数据质量检查
--   利：分类口径干净，类别数可控；检查规则（每天跑本 SQL，结果非空就告警）能及时发现新类别。
--   弊：信息损失：pc_gamer 这类有明确含义的类别被抹掉，影响按品类的分析。
--
-- 对 610 个类别为空的商品：它们的名称长度、描述长度、图片数也同时为空（见 load_olist.py 注释），
-- 是商品资料整体缺失，翻译无从谈起。应归入 'unknown'，并反馈给商品资料的维护方，而不是补翻译。
-- ============================================================
