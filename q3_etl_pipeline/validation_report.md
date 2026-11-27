# Q3 数据校验报告

本文件由 `pipeline.py` 自动生成，每次运行覆盖。本次运行耗时 4.8 秒。
内容哈希相同即说明两次运行输出的数据完全一致（可重现性检查）。

## 从原始数据到 sales_facts

| 步骤 | 行数 |
| --- | --- |
| 原始行数 | 1,067,371 |
| 去重后 | 1,033,036 |
| 其中候选销售行（数字发票、商品代码、数量与单价为正） | 1,003,417 |
| 剔除 1 小时内被撤销的原单后（sales_facts） | 1,002,424 |

## 输出 1：sales_facts

行数：1,002,424；内容哈希：`c351aba1038b901c`

| 列 | 类型 | 空值率 |
| --- | --- | --- |
| invoice_no | string | 0.00% |
| stock_code | string | 0.00% |
| description | string | 0.00% |
| quantity | int64 | 0.00% |
| unit_price | float64 | 0.00% |
| total_amount | float64 | 0.00% |
| invoice_datetime | datetime64[ns] | 0.00% |
| customer_id | string | 22.63% |
| country | string | 0.00% |

| 字段 | 最小 | 25% | 中位数 | 75% | 99% | 最大 | 均值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| quantity | 1.00 | 1.00 | 4.00 | 12.00 | 104.00 | 19,152.00 | 10.97 |
| unit_price | 0.03 | 1.25 | 2.10 | 4.13 | 16.95 | 1,157.15 | 3.34 |
| total_amount | 0.06 | 4.13 | 10.08 | 17.70 | 179.00 | 15,818.40 | 19.25 |

金额合计 19,294,300.29；发票 39,424 张；商品 4,726 个；无客户号的行占 22.63%；时间范围 2009-12-01 07:45:00 至 2011-12-09 12:50:00。

国家 TOP 5（按金额）：

| 国家 | 金额 | 占比 |
| --- | --- | --- |
| United Kingdom | 16,462,414.53 | 85.32% |
| EIRE | 615,318.88 | 3.19% |
| Netherlands | 549,773.41 | 2.85% |
| Germany | 383,169.10 | 1.99% |
| France | 309,853.49 | 1.61% |

## 输出 2：customer_features

行数：5,851；内容哈希：`03d457d8751f48c1`

| 列 | 类型 | 空值率 |
| --- | --- | --- |
| customer_id | string | 0.00% |
| recency_days | int64 | 0.00% |
| frequency | int64 | 0.00% |
| monetary | float64 | 0.00% |
| first_purchase_date | datetime64[ns] | 0.00% |
| last_purchase_date | datetime64[ns] | 0.00% |
| unique_products | int64 | 0.00% |
| country | string | 0.00% |
| is_one_time_buyer | bool | 0.00% |
| returns_amount | float64 | 0.00% |
| net_monetary | float64 | 0.00% |

| 字段 | 最小 | 25% | 中位数 | 75% | 99% | 最大 | 均值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| recency_days | 0.00 | 25.00 | 95.00 | 379.00 | 726.00 | 738.00 | 199.76 |
| frequency | 1.00 | 1.00 | 3.00 | 7.00 | 45.50 | 371.00 | 6.24 |
| monetary | 2.90 | 338.14 | 853.88 | 2,233.21 | 26,781.78 | 580,987.04 | 2,857.16 |
| unique_products | 1.00 | 19.00 | 45.00 | 103.00 | 502.50 | 2,546.00 | 82.15 |
| returns_amount | 0.00 | 0.00 | 0.00 | 24.77 | 913.25 | 22,998.40 | 67.72 |
| net_monetary | -1,343.24 | 331.12 | 840.24 | 2,169.28 | 26,319.02 | 578,408.64 | 2,789.45 |

客户 5,851 个；只买过一次的占 27.72%；net_monetary 为负（退货多于数据期内购买）的客户 3 个。

## 输出 3：returns_log

行数：19,104；内容哈希：`cc7857af62df8513`

| 列 | 类型 | 空值率 |
| --- | --- | --- |
| invoice_no | string | 0.00% |
| stock_code | string | 0.00% |
| description | string | 0.00% |
| quantity | int64 | 0.00% |
| unit_price | float64 | 0.00% |
| total_amount | float64 | 0.00% |
| invoice_datetime | datetime64[ns] | 0.00% |
| customer_id | string | 3.74% |
| country | string | 0.00% |
| matched_original_invoice | string | 17.42% |
| match_status | string | 0.00% |
| is_quick_reversal | bool | 0.00% |

| match_status | 行数 | 占比 |
| --- | --- | --- |
| 匹配：部分退货 | 9,075 | 47.50% |
| 匹配：同数量 | 5,993 | 31.37% |
| 未匹配：非商品代码 | 1,184 | 6.20% |
| 未匹配：数据期内无该客户该商品的购买 | 1,034 | 5.41% |
| 匹配：部分退货（单价不同） | 599 | 3.14% |
| 未匹配：该商品的购买都晚于退货 | 496 | 2.60% |
| 未匹配：无客户号 | 329 | 1.72% |
| 未匹配：退货数量大于任何一笔更早的购买 | 195 | 1.02% |
| 匹配：同数量（单价不同） | 105 | 0.55% |
| 未匹配：可用原单的剩余数量已被其他退货占满 | 89 | 0.47% |
| 匹配：手工冲销（金额相同） | 5 | 0.03% |

匹配成功 15,777 行（82.58%）；其中判定为 1 小时内撤销的 993 行，对应原单已从 sales_facts 中剔除。

## 断言结果

| 断言 | 结果 |
| --- | --- |
| sales_facts 非空 | 通过 |
| sales_facts 字段与题目一致 | 通过 |
| sales_facts.total_amount > 0 | 通过 |
| sales_facts.quantity > 0 | 通过 |
| sales_facts.unit_price > 0 | 通过 |
| sales_facts.total_amount = quantity × unit_price | 通过 |
| sales_facts 不含 C/A 等非数字发票 | 通过 |
| sales_facts 无完全重复行 | 通过 |
| sales_facts 关键字段无空值 | 通过 |
| sales_facts.invoice_datetime 是日期类型 | 通过 |
| sales_facts ID 列是字符串类型 | 通过 |
| customer_features 每个客户一行 | 通过 |
| customer_features 无空客户号 | 通过 |
| recency_days >= 0 | 通过 |
| frequency >= 1 | 通过 |
| monetary > 0 | 通过 |
| first_purchase_date <= last_purchase_date | 通过 |
| is_one_time_buyer 与 frequency == 1 一致 | 通过 |
| customer_features 客户数 = sales_facts 中有客户号的客户数 | 通过 |
| monetary 合计 = sales_facts 有客户号的金额合计 | 通过 |
| returns_log 只含 C 发票 | 通过 |
| returns_log 中除非商品代码外数量都 < 0 | 通过 |
| 已匹配行都有原单号、未匹配行原单号为空 | 通过 |
| returns_log.matched_original_invoice 是字符串类型 | 通过 |
| 原单号都是数字发票 | 通过 |
| 撤销行一定是同数量同单价匹配，或金额相同的手工冲销 | 通过 |

另有断言：每笔原单的累计退货数量不超过购买数量，且原单都早于退货（`check_match_capacity()`）。
