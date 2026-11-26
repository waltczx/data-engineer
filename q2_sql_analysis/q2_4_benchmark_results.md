# Q2.4 实测结果

本文件由 `q2_4_benchmark.py` 生成，每个数据库一节，重新运行会替换对应的一节。

## 数据库 `olist`：订单 99,441 行

四张表合计 84 MB，InnoDB 缓冲池 128 MB；每种情况运行 5 次取耗时中位数。

| 索引 | SQL | 耗时（毫秒） | 相对原始 SQL | 读取行数 | 订单数 | 收入合计 |
|---|---|---|---|---|---|---|
| 默认索引（主键 + 外键） | 原始 SQL | 742 | 100% | 286,255 | 20,367 | 2,547,698.71 |
| 默认索引（主键 + 外键） | 改写 SQL | 725 | 98% | 598,152 | 20,367 | 2,535,313.89 |
| 默认 + 推荐的 2 个索引 | 原始 SQL | 488 | 66% | 286,255 | 20,367 | 2,547,698.71 |
| 默认 + 推荐的 2 个索引 | 改写 SQL | 391 | 53% | 254,119 | 20,367 | 2,535,313.89 |

改写 SQL 在推荐索引下的执行计划：

```
-> Sort: revenue DESC
    -> Table scan on <temporary>
        -> Aggregate using temporary table
            -> Nested loop left join  (cost=100365 rows=19597)
                -> Nested loop left join  (cost=73664 rows=9798)
                    -> Nested loop inner join  (cost=60313 rows=4899)
                        -> Nested loop inner join  (cost=25182 rows=97984)
                            -> Table scan on sc  (cost=6521..6577 rows=4347)
                                -> Materialize  (cost=6520..6520 rows=4347)
                                    -> Filter: (customers.customer_city like '%sao%')  (cost=6086 rows=4347)
                                        -> Covering index skip scan for deduplication on customers using idx_customers_city  (cost=6086 rows=4347)
                            -> Covering index lookup on c using idx_customers_city (customer_city=sc.customer_city)  (cost=2.03 rows=22.5)
                        -> Invalidate materialized tables (row from o)  (cost=0.256 rows=0.05)
                            -> Invalidate materialized tables (row from o)  (cost=0.256 rows=0.05)
                                -> Filter: ((o.order_status = 'delivered') and (o.order_purchase_timestamp >= TIMESTAMP'2017-01-01 00:00:00'))  (cost=0.256 rows=0.05)
                                    -> Index lookup on o using idx_orders_customer (customer_id=c.customer_id)  (cost=0.256 rows=1.02)
                    -> Table scan on i  (cost=2.53..2.53 rows=1)
                        -> Materialize (invalidate on row from o)  (cost=0.679..0.679 rows=2)
                            -> Aggregate: sum(oi.price)  (cost=0.479 rows=1)
                                -> Index lookup on oi using PRIMARY (order_id=o.order_id)  (cost=0.365 rows=1.15)
                -> Table scan on r  (cost=2.53..2.53 rows=1)
                    -> Materialize (invalidate on row from o)  (cost=0.552..0.552 rows=2)
                        -> Limit: 1 row(s)  (cost=0.352 rows=1)
                            -> Sort: rv.review_answer_timestamp DESC, rv.review_id, limit input to 1 row(s) per chunk  (cost=0.352 rows=1)
                                -> Index lookup on rv using idx_reviews_order (order_id=o.order_id)  (cost=0.352 rows=1)

```

## 数据库 `olist_x10`：订单 994,410 行

四张表合计 1326 MB，InnoDB 缓冲池 128 MB；每种情况运行 3 次取耗时中位数。

| 索引 | SQL | 耗时（毫秒） | 相对原始 SQL | 读取行数 | 订单数 | 收入合计 |
|---|---|---|---|---|---|---|
| 默认索引（主键 + 外键） | 原始 SQL | 15,459 | 100% | 2,862,523 | 203,670 | 25,476,987.10 |
| 默认索引（主键 + 外键） | 改写 SQL | 16,523 | 107% | 5,979,045 | 203,670 | 25,353,138.90 |
| 默认 + 推荐的 2 个索引 | 原始 SQL | 13,191 | 85% | 2,862,523 | 203,670 | 25,476,987.10 |
| 默认 + 推荐的 2 个索引 | 改写 SQL | 9,609 | 62% | 2,499,457 | 203,670 | 25,353,138.90 |

改写 SQL 在推荐索引下的执行计划：

```
-> Sort: revenue DESC
    -> Table scan on <temporary>
        -> Aggregate using temporary table
            -> Nested loop left join  (cost=1.74e+6 rows=215528)
                -> Nested loop left join  (cost=1.45e+6 rows=107764)
                    -> Nested loop inner join  (cost=1.3e+6 rows=53882)
                        -> Nested loop inner join  (cost=165656 rows=1.08e+6)
                            -> Table scan on sc  (cost=4052..4087 rows=2614)
                                -> Materialize  (cost=4052..4052 rows=2614)
                                    -> Filter: (customers.customer_city like '%sao%')  (cost=3790 rows=2614)
                                        -> Covering index skip scan for deduplication on customers using idx_customers_city  (cost=3790 rows=2614)
                            -> Covering index lookup on c using idx_customers_city (customer_city=sc.customer_city)  (cost=20.6 rows=412)
                        -> Invalidate materialized tables (row from o)  (cost=0.951 rows=0.05)
                            -> Invalidate materialized tables (row from o)  (cost=0.951 rows=0.05)
                                -> Filter: ((o.order_status = 'delivered') and (o.order_purchase_timestamp >= TIMESTAMP'2017-01-01 00:00:00'))  (cost=0.951 rows=0.05)
                                    -> Index lookup on o using idx_orders_customer (customer_id=c.customer_id)  (cost=0.951 rows=1)
                    -> Table scan on i  (cost=2.53..2.53 rows=1)
                        -> Materialize (invalidate on row from o)  (cost=1.39..1.39 rows=2)
                            -> Aggregate: sum(oi.price)  (cost=1.19 rows=1)
                                -> Index lookup on oi using PRIMARY (order_id=o.order_id)  (cost=1.08 rows=1.14)
                -> Table scan on r  (cost=2.53..2.53 rows=1)
                    -> Materialize (invalidate on row from o)  (cost=1.32..1.32 rows=2)
                        -> Limit: 1 row(s)  (cost=1.12 rows=1)
                            -> Sort: rv.review_answer_timestamp DESC, rv.review_id, limit input to 1 row(s) per chunk  (cost=1.12 rows=1.15)
                                -> Covering index lookup on rv using idx_reviews_order_answer (order_id=o.order_id)  (cost=1.12 rows=1.15)

```
