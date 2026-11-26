"""Q2 一键运行：确保数据已导入 MySQL，再依次执行 3 个 SQL 文件，结果写入 q2_results.md。

用法：python q2_sql_analysis/run_sql.py
连接参数见 load_olist.py（环境变量 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE）。
Q2.4 的性能实测单独运行：python q2_sql_analysis/q2_4_benchmark.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_olist import connect, ensure_loaded  # noqa: E402

HERE = Path(__file__).resolve().parent
SQL_FILES = ["q2_1_translation_gap.sql", "q2_2_monthly_metrics.sql", "q2_3_top_sellers.sql"]
RESULT_PATH = HERE / "q2_results.md"


def strip_comments(sql: str) -> str:
    """去掉整行注释（以 -- 开头的行）。文件里只有一条查询语句。"""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")).strip().rstrip(";")


def to_markdown(headers: list[str], rows: list[tuple]) -> str:
    fmt = lambda v: "NULL" if v is None else str(v)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def main() -> int:
    ensure_loaded()
    conn = connect()
    sections = [
        "# Q2 查询结果",
        "",
        f"本文件由 `run_sql.py` 生成（{datetime.now():%Y-%m-%d %H:%M}），每次运行覆盖。口径说明见各 .sql 文件开头的注释。",
        "",
    ]
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        sections += [f"MySQL 版本：{cur.fetchone()[0]}", ""]
        for name in SQL_FILES:
            sql = strip_comments((HERE / name).read_text(encoding="utf-8"))
            start = time.perf_counter()
            cur.execute(sql)
            rows = cur.fetchall()
            elapsed = time.perf_counter() - start
            headers = [d[0] for d in cur.description]
            print(f"{name}：{len(rows)} 行，{elapsed:.2f} 秒")
            sections += [f"## {name}", "", f"{len(rows)} 行，耗时 {elapsed:.2f} 秒。", "", to_markdown(headers, rows), ""]
    conn.close()
    RESULT_PATH.write_text("\n".join(sections), encoding="utf-8")
    print(f"已写入 {RESULT_PATH.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
