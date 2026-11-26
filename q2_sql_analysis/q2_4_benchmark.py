"""Q2.4 慢查询优化的实测：比较原始 SQL 与改写后 SQL 在两种索引配置下的耗时和读取行数。

用法：
    python q2_sql_analysis/q2_4_benchmark.py                                   # 原始数据（olist 库）
    MYSQL_DATABASE=olist_x10 python q2_sql_analysis/q2_4_benchmark.py --skip-load   # 10 倍数据
10 倍数据库用 q2_4_build_x10.sql 生成（需要建库权限，约 6 分钟）。

结果追加写入 q2_4_benchmark_results.md（按数据库分节）。运行结束后把索引恢复成 load_olist.py 的默认状态。

读取行数 = 查询前后 Handler_read_* 计数器之差：它统计存储引擎实际读了多少行（含索引查找和全表扫描），
比 EXPLAIN 的估算更接近真实工作量。用差值而不是 FLUSH STATUS 清零，是因为后者需要 RELOAD 权限。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_olist import connect, connection_settings, ensure_loaded  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULT_PATH = HERE / "q2_4_benchmark_results.md"

ORIGINAL_SQL = """
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
ORDER BY revenue DESC
"""

# 改写要点（详见 q2_4_query_optimization.md）：
# 1. 先从约 4,000 个不同城市名里找出匹配的城市（走城市索引），再按城市取客户、按客户取订单：
#    最有选择性的条件（城市，约 21% 的客户）最先执行；"已送达且 2017 年以后"保留了约 80% 的订单，选择性很差。
# 2. 商品金额和评价用 LATERAL 按订单各取一行：每个订单只出现一次，不再出现"商品 × 评价"的笛卡尔放大，
#    SUM 不会重复计算，也不需要 COUNT(DISTINCT)。
# 3. 去掉 LOWER()：数据里的城市名本来就是小写无重音（无任何大写字母），LOWER 只会让索引失效。
# 城市条件保持 '%sao%' 不变，只比较性能；该条件本身的语义问题见文档。
OPTIMIZED_SQL = """
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
ORDER BY revenue DESC
"""

RECOMMENDED_INDEXES = {
    # 城市在前：DISTINCT 城市名可以只扫索引；按城市取客户时，state 和主键 customer_id 都在索引里，不用回表。
    "idx_customers_city": ("customers", "CREATE INDEX idx_customers_city ON customers (customer_city, customer_state)"),
    # 覆盖索引：按订单找最晚的一条评价，排序列和分数都在索引里，不用回表、不用额外排序。
    "idx_reviews_order_answer": ("order_reviews",
                                 "CREATE INDEX idx_reviews_order_answer ON order_reviews "
                                 "(order_id, review_answer_timestamp, review_score)"),
}


def handler_reads(cur) -> int:
    cur.execute("SHOW SESSION STATUS LIKE 'Handler_read%'")
    return sum(int(value) for _, value in cur.fetchall())


def measure(cur, sql: str, runs: int) -> dict:
    """运行 runs 次，取耗时中位数；读取行数取最后一次。"""
    timings, rows, result = [], 0, ()
    for _ in range(runs):
        before = handler_reads(cur)
        start = time.perf_counter()
        cur.execute(sql)
        result = cur.fetchall()
        timings.append(time.perf_counter() - start)
        rows = handler_reads(cur) - before
    return {"ms": statistics.median(timings) * 1000, "rows_read": rows,
            "revenue": sum(float(row[2] or 0) for row in result), "orders": sum(int(row[1]) for row in result)}


def set_recommended(cur, enabled: bool) -> None:
    for name, (table, statement) in RECOMMENDED_INDEXES.items():
        cur.execute(f"SHOW INDEX FROM {table}")
        exists = name in {row[2] for row in cur.fetchall()}
        if enabled and not exists:
            cur.execute(statement)
        elif not enabled and exists:
            cur.execute(f"DROP INDEX {name} ON {table}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Q2.4 慢查询实测")
    parser.add_argument("--skip-load", action="store_true", help="不检查/导入数据（用于 10 倍数据库）")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()
    if not args.skip_load:
        ensure_loaded()

    database = connection_settings()["database"]
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders")
    order_count = cur.fetchone()[0]
    cur.execute("SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024) FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN ('orders','customers','order_items','order_reviews')")
    size_mb = cur.fetchone()[0]
    cur.execute("SELECT @@innodb_buffer_pool_size / 1024 / 1024")
    pool_mb = cur.fetchone()[0]

    scenarios = []
    try:
        set_recommended(cur, False)
        scenarios.append(("默认索引（主键 + 外键）", "原始 SQL", measure(cur, ORIGINAL_SQL, args.runs)))
        scenarios.append(("默认索引（主键 + 外键）", "改写 SQL", measure(cur, OPTIMIZED_SQL, args.runs)))
        set_recommended(cur, True)
        scenarios.append(("默认 + 推荐的 2 个索引", "原始 SQL", measure(cur, ORIGINAL_SQL, args.runs)))
        scenarios.append(("默认 + 推荐的 2 个索引", "改写 SQL", measure(cur, OPTIMIZED_SQL, args.runs)))
        cur.execute("EXPLAIN FORMAT=TREE " + OPTIMIZED_SQL)
        plan = cur.fetchone()[0]
    finally:
        set_recommended(cur, False)  # 恢复默认索引，不影响其他题目
        conn.commit()

    base = scenarios[0][2]
    lines = [
        f"## 数据库 `{database}`：订单 {order_count:,} 行",
        "",
        f"四张表合计 {size_mb} MB，InnoDB 缓冲池 {pool_mb:.0f} MB；每种情况运行 {args.runs} 次取耗时中位数。",
        "",
        "| 索引 | SQL | 耗时（毫秒） | 相对原始 SQL | 读取行数 | 订单数 | 收入合计 |",
        "|---|---|---|---|---|---|---|",
        *[f"| {index} | {name} | {m['ms']:,.0f} | {m['ms'] / base['ms']:.0%} | {m['rows_read']:,} | {m['orders']:,} | {m['revenue']:,.2f} |"
          for index, name, m in scenarios],
        "",
        "改写 SQL 在推荐索引下的执行计划：",
        "",
        "```",
        plan,
        "```",
        "",
    ]
    previous = RESULT_PATH.read_text(encoding="utf-8") if RESULT_PATH.is_file() else ""
    header = "# Q2.4 实测结果\n\n本文件由 `q2_4_benchmark.py` 生成，每个数据库一节，重新运行会替换对应的一节。\n\n"
    sections = [s for s in previous.split("\n## ")[1:] if not s.startswith(f"数据库 `{database}`")]
    body = "".join("## " + s if not s.startswith("## ") else s for s in sections)
    RESULT_PATH.write_text(header + body.rstrip("\n") + ("\n\n" if body else "") + "\n".join(lines), encoding="utf-8")
    for index, name, m in scenarios:
        print(f"{index:<20} {name:<6} {m['ms']:9,.0f} ms  读取 {m['rows_read']:>11,} 行  收入 {m['revenue']:,.2f}")
    print(f"已写入 {RESULT_PATH.name}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
