# Q2.4 - 慢查询优化分析

## 原始 SQL

```sql
SELECT 
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS orders,
    SUM(oi.price) AS revenue,
    AVG(r.review_score) AS avg_score
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id
LEFT JOIN order_items oi ON o.order_id = oi.order_id
LEFT JOIN order_reviews r ON o.order_id = r.order_id
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
  AND LOWER(c.customer_city) LIKE '%sao%'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

下文所有数字都是在 MySQL 8.0.46 上实测的，可用 `python q2_sql_analysis/q2_4_benchmark.py` 复现（完整结果见 `q2_4_benchmark_results.md`）。为了验证"数据量增长后变慢"，我另外用 `q2_4_build_x10.sql` 生成了 10 倍数据的测试库：约 99 万订单、113 万商品行、99 万评价、99 万客户。

---

## 第 1 问：性能问题分析（至少 3 个）

### 问题 1：城市条件用不上索引，每次都要扫描全部客户

`LOWER(c.customer_city) LIKE '%sao%'` 有两处问题：

- **对列套函数**：B-Tree 索引里存的是原值，`LOWER(列)` 之后的值不在索引里，所以优化器无法用索引定位。
- **前导通配符**：`'%sao%'` 以 `%` 开头，B-Tree 只能按前缀查找，无法定位"中间包含 sao"的值。

后果是每次查询都要读一遍全部客户行并逐行计算。客户数随订单线性增长（Olist 每个订单生成一个新的 customer_id），100 倍数据时就是约 1,000 万行全扫描。

而且 `LOWER` 在这份数据里**完全多余**：实测 customers 表中没有任何一个城市名含大写字母（`SUM(BINARY customer_city <> LOWER(customer_city)) = 0`），城市名在源头就已经是小写、去重音的形式（"sao paulo"）。

### 问题 2：三表直接关联造成行数放大，GROUP BY 和 COUNT(DISTINCT) 要处理的中间结果被放大

orders 和 order_items 是一对多，orders 和 order_reviews 也是一对多。三张表直接关联时，一个订单会变成"商品行数 × 评价条数"行。之后 `COUNT(DISTINCT o.order_id)` 需要在临时表里对这些放大后的行去重，`GROUP BY` 也要处理全部放大后的行。

在原始数据中放大倍数不大（平均每单 1.13 个商品行），但它与数据量线性相关，并且只要有大订单（数据里最多一单 21 个商品行）乘上多条评价，中间结果就会成倍增加。这个问题同时也是第 3 问统计错误的根源。
把订单同时关联到商品明细和评价。两张表对同一订单都可能有多行，于是行数会相乘。

例如一笔订单有两件商品，价格分别是 100、50 元，还有两条评价：
### 问题 3：最有选择性的条件最后才起作用，大量订单被白白读取

三个过滤条件的选择性差别很大（原始数据实测）：

| 条件 | 保留比例 |
|---|---|
| `order_status = 'delivered'` 且 `order_purchase_timestamp >= '2017-01-01'` | 约 80% 的订单 |
| 城市包含 "sao" | 约 21% 的客户 |

订单上的两个条件几乎不过滤任何数据，所以即使在 `(order_status, order_purchase_timestamp)` 上建索引，优化器也会选择全表扫描。这是只看 SQL 很容易误判的地方："WHERE 里的列都加索引"在这里没有用。真正有选择性的是城市，但原 SQL 中它用不上索引（问题 1），只能在关联完订单之后才过滤。结果是 80% 的订单都被读出、关联，最后再丢掉其中的大部分。

### 问题 4：数据超出内存后，全表扫描和随机读取变成磁盘 I/O，耗时超线性增长

这是"2 秒变 5 分钟"的直接原因，实测如下：

| 数据量 | 四张表大小 | InnoDB 缓冲池 | 原始 SQL 耗时 |
|---|---|---|---|
| 1 倍 | 84 MB | 128 MB | 约 0.5 秒 |
| 10 倍 | 1,326 MB | 128 MB | 约 13–15 秒 |

数据只增长了 10 倍，耗时却增长了约 30 倍。原因是 1 倍时四张表能全部放进内存缓冲池，10 倍时放不下，扫描和按主键的随机查找都变成磁盘读取。Olist 的各种 ID 都是 32 位随机十六进制串，按主键插入和查找本来就是随机访问，内存不够时尤其吃亏。按这个趋势，100 倍数据（约 13 GB）在默认配置下达到分钟级是合理的。

### 其他次要问题

- `LEFT JOIN customers` 再在 WHERE 里过滤 `c.customer_city`，实际效果等于内连接（城市为 NULL 的行会被 WHERE 排除）。MySQL 优化器会自动把它转成内连接，所以不影响性能，但写法会误导读者，以为没有客户信息的订单也会被保留。
- `ORDER BY revenue` 只对最多 27 个州排序，代价可以忽略。

---

## 第 2 问：优化方案

### 优化后的 SQL

```sql
SELECT
    c.customer_state,
    COUNT(*)            AS orders,
    SUM(i.revenue)      AS revenue,
    AVG(r.review_score) AS avg_score
FROM (SELECT DISTINCT customer_city FROM customers WHERE customer_city LIKE '%sao%') AS sc
JOIN customers AS c ON c.customer_city = sc.customer_city
JOIN orders    AS o ON o.customer_id   = c.customer_id
LEFT JOIN LATERAL (
    SELECT SUM(oi.price) AS revenue
    FROM order_items AS oi
    WHERE oi.order_id = o.order_id
) AS i ON TRUE
LEFT JOIN LATERAL (
    SELECT rv.review_score                     -- 一个订单有多条评价时，取最晚回复的一条
    FROM order_reviews AS rv
    WHERE rv.order_id = o.order_id
    ORDER BY rv.review_answer_timestamp DESC, rv.review_id
    LIMIT 1
) AS r ON TRUE
WHERE o.order_status = 'delivered'
  AND o.order_purchase_timestamp >= '2017-01-01'
GROUP BY c.customer_state
ORDER BY revenue DESC;
```

改写思路：

1. **先找城市，再找客户**：不同的城市名只有约 4,100 个。在城市索引上扫出匹配的城市名，再用这些城市名按索引取客户，最后用客户的 `customer_id` 按索引取订单。最有选择性的条件变成了第一步。前导通配符仍然无法按前缀定位，但扫描对象从全部客户行变成了一个很小的索引。
2. **商品和评价各自按订单取一行（LATERAL）**：每个订单只出现一次，没有行数放大，也就不需要 `COUNT(DISTINCT)`。商品金额走 order_items 的主键 `(order_id, order_item_id)` 前缀，评价走下面的覆盖索引。
3. **去掉 `LOWER()`**：数据本身没有大写字母。如果担心以后的数据会有大写，应该在写入时统一规范化，而不是在每次查询时计算。
4. 只改写 SQL 而不加索引是没有用的：实测改写 SQL 在默认索引下反而读取了更多行（见下表）。必须和下面两个索引一起使用。

### 建议添加的索引

```sql
-- 城市在前：取不重复城市名时只扫这个索引；按城市取客户时，customer_state 和主键都在索引里，不用回表
CREATE INDEX idx_customers_city ON customers (customer_city, customer_state);

-- 覆盖索引：按订单找最晚的一条评价，排序列和分数都在索引里，不用回表，也不用额外排序
CREATE INDEX idx_reviews_order_answer ON order_reviews (order_id, review_answer_timestamp, review_score);

-- 已有、必须保留：按客户取订单
-- CREATE INDEX idx_orders_customer ON orders (customer_id);
```

没有推荐 `(order_status, order_purchase_timestamp)` 索引：如上文问题 3 所述，这两个条件保留了约 80% 的订单，实测优化器不会使用它。

### 优化前 vs 优化后实测对比

在同样建好推荐索引的前提下比较（对原始 SQL 来说这两个索引用不上，它读取的行数没有变化）：

| 指标 | 优化前 | 优化后 |
|------|--------|--------|
| 执行时间 | 488 毫秒（1 倍数据）；13.2 秒（10 倍数据） | 391 毫秒（-20%）；9.6 秒（-27%） |
| 扫描行数 | 286,255（1 倍数据）；2,862,523（10 倍数据） | 254,119（-11%）；2,499,457（-13%） |

扫描行数 = 查询前后 `Handler_read_*` 计数器之差，即存储引擎实际读取的行数（含索引查找）。1 倍时每种情况运行 5 次、10 倍时 3 次，取耗时中位数。

### 坦白说：这些优化不足以解决 100 倍的问题

实测 SQL 改写加索引只带来约 20–30% 的提升，数据量越大提升越明显，但远远不能把 5 分钟降到秒级。原因是查询本身要汇总约 20% 的客户及其订单，需要读取的数据量与总数据量成正比，索引只能减少"读了又丢掉"的那部分。

数据量增长 100 倍后，真正有效的办法是**不要在明细表上实时汇总**：

1. **预聚合**：由 ETL 每天（或每小时）把已送达订单汇总到 `(日期, 州, 城市)` 粒度的汇总表，订单数、收入、评分合计和评分条数分别存列。查询时只扫这张小表，城市条件在几千行上执行，毫秒级返回。
2. **把城市匹配变成维度属性**：在城市维度表上预先标记"是否为圣保罗类城市"（同时修正下面的语义问题），汇总表按这个标记分组，查询时就不需要 LIKE 了。
3. **报表查询走 OLAP 引擎**（如 ClickHouse 或 Doris）：列式存储加向量化执行，适合这种"大范围扫描 + 聚合"的查询，并且不和线上 MySQL 争资源。

### 附：城市条件本身可能写错了

`'%sao%'` 的本意应该是"São Paulo、São José 这类以 São 开头的城市"，但它是子串匹配。葡萄牙语中以 "-são" 结尾的词很多，所以也会匹配到 Promissão（32 个客户）、Lagoa da Confusão、Travessão 这类城市。在本题的条件下，按单词边界匹配（`customer_city REGEXP '(^| )sao( |$)'`）的订单数为 20,326，比 `'%sao%'` 少 41 单。如果需求其实只是圣保罗市，应该用 `customer_city = 'sao paulo'`（15,014 单），这样还能直接走索引。这一点需要和提需求的人确认。上面的性能对比为了公平，保持了原来的 `'%sao%'`。

---

## 第 3 问：潜在的统计错误（关键题）

> `SUM(oi.price)` 的结果可能不是你以为的「订单总收入」。为什么？

**原因：一个订单有多条评价时，它的每个商品行都会和每条评价各配对一次，商品金额被重复累加。**

三张表直接关联后，一个有 2 件商品、2 条评价的订单会产生 2 × 2 = 4 行，`SUM(oi.price)` 把每个商品的价格加了 2 次。`COUNT(DISTINCT o.order_id)` 有 DISTINCT 保护，订单数不受影响，所以这个错误很隐蔽：订单数是对的，收入却偏大。

数据里确实存在这种订单：547 个订单有 2–3 条评价。本题的过滤条件下有 113 个这样的订单，实测：

| | 收入合计 |
|---|---|
| 原始 SQL（商品 × 评价） | 2,547,698.71 |
| 正确值（商品按订单汇总，不经过评价表） | 2,535,313.89 |
| 多算 | 12,384.82（+0.49%） |

同样的问题也影响 `AVG(r.review_score)`：评分按"商品行 × 评价"的行数加权，商品多的订单的评分被重复计入，平均分偏向大订单。

另外两个口径问题（不是重复计算，但同样会让结果"不是你以为的订单总收入"）：

1. **不含运费**：`SUM(oi.price)` 只算商品价，不含 `freight_value`。Q2.2 中 GMV 的口径包含运费，两个报表的收入会对不上。
2. **不是实际支付金额**：客户实际支付的金额在 order_payments 表，可能因分期利息、优惠券等与商品价加运费不同。"收入"到底指商品价、GMV 还是实收，需要先明确。

**修正方案**：

```sql
-- 关键是先把一对多的表按订单汇总成一行，再与订单关联（或用上面的 LATERAL 写法）
WITH target_orders AS (
    SELECT o.order_id, c.customer_state
    FROM orders AS o
    JOIN customers AS c ON c.customer_id = o.customer_id
    WHERE o.order_status = 'delivered'
      AND o.order_purchase_timestamp >= '2017-01-01'
      AND c.customer_city LIKE '%sao%'
),
order_revenue AS (                      -- 每个订单一行
    SELECT order_id, SUM(price) AS revenue   -- 如果收入口径包含运费，改为 SUM(price + freight_value)
    FROM order_items
    GROUP BY order_id
),
order_score AS (                        -- 每个订单一行：取最晚回复的那条评价
    SELECT order_id, review_score
    FROM (
        SELECT order_id, review_score,
               ROW_NUMBER() OVER (PARTITION BY order_id
                                  ORDER BY review_answer_timestamp DESC, review_id) AS rn
        FROM order_reviews
    ) AS x
    WHERE rn = 1
)
SELECT
    t.customer_state,
    COUNT(*)            AS orders,
    SUM(v.revenue)      AS revenue,
    AVG(s.review_score) AS avg_score    -- 每个订单只算一次
FROM target_orders AS t
LEFT JOIN order_revenue AS v ON v.order_id = t.order_id
LEFT JOIN order_score   AS s ON s.order_id = t.order_id
GROUP BY t.customer_state
ORDER BY revenue DESC;
```

这个 CTE 版本最容易读懂，适合说明修正逻辑。但实测它在 MySQL 中比第 2 问的 LATERAL 版本慢得多：1 倍数据约 1.5–1.6 秒，LATERAL 版本约 0.3–0.5 秒。原因是 order_revenue 和 order_score 会先对全表的商品和评价做汇总，而不是只汇总目标订单。所以生产使用推荐 LATERAL 版本，两者结果完全相同（收入、订单数、各州平均分逐项一致）。
