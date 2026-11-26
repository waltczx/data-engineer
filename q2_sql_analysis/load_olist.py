"""把 Olist 9 张 CSV 导入 MySQL 8.0。

用法：
    python q2_sql_analysis/load_olist.py            # 表已存在且行数正确就跳过
    python q2_sql_analysis/load_olist.py --force    # 删除重建

连接信息从环境变量读取（也可写在仓库根目录 .env 里），默认值对应 README 中的 Docker 命令：
    MYSQL_HOST=127.0.0.1  MYSQL_PORT=3306  MYSQL_USER=root  MYSQL_PASSWORD=root  MYSQL_DATABASE=olist

为什么手写建表语句，而不用 pandas.to_sql 自动建表：
1. to_sql 会把所有字符串建成 TEXT，不能建主键和普通索引，JOIN 很慢（Q2.4 讨论的正是索引问题）。
2. 邮编前缀有 23,995 个客户以 0 开头（如 01151），按数字读入会丢掉前导 0，必须存成字符。
3. 时间列需要是 DATETIME，SQL 里才能直接做日期运算。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pymysql

ROOT = Path(__file__).resolve().parents[1]
OLIST_DIR = ROOT / "data" / "olist"
CHUNK = 5_000
PRODUCT_INT_COLUMNS = {
    "product_name_lenght", "product_description_lenght", "product_photos_qty",
    "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm",
}

# 表名使用 Q2.4 题目中的名字（orders、customers、order_items、order_reviews），其余按同样规则命名。
# 每项：(表名, CSV 文件名, 建表语句, 期望行数)
TABLES = [
    ("customers", "olist_customers_dataset.csv", """
        CREATE TABLE customers (
            customer_id              CHAR(32)    NOT NULL,
            customer_unique_id       CHAR(32)    NOT NULL,   -- 同一个人在不同订单里 customer_id 不同，识别人要用这一列
            customer_zip_code_prefix CHAR(5)     NOT NULL,   -- 字符：保留前导 0
            customer_city            VARCHAR(64) NOT NULL,
            customer_state           CHAR(2)     NOT NULL,
            PRIMARY KEY (customer_id)
        )""", 99_441),
    ("sellers", "olist_sellers_dataset.csv", """
        CREATE TABLE sellers (
            seller_id              CHAR(32)    NOT NULL,
            seller_zip_code_prefix CHAR(5)     NOT NULL,
            seller_city            VARCHAR(64) NOT NULL,
            seller_state           CHAR(2)     NOT NULL,
            PRIMARY KEY (seller_id)
        )""", 3_095),
    ("product_category_name_translation", "product_category_name_translation.csv", """
        CREATE TABLE product_category_name_translation (
            product_category_name         VARCHAR(64) NOT NULL,
            product_category_name_english VARCHAR(64) NOT NULL,
            PRIMARY KEY (product_category_name)
        )""", 71),
    ("products", "olist_products_dataset.csv", """
        CREATE TABLE products (
            product_id                 CHAR(32)    NOT NULL,
            product_category_name      VARCHAR(64) NULL,     -- 610 个商品为空
            product_name_lenght        INT NULL,             -- 列名拼写沿用原始数据（lenght）
            product_description_lenght INT NULL,
            product_photos_qty         INT NULL,
            product_weight_g           INT NULL,
            product_length_cm          INT NULL,
            product_height_cm          INT NULL,
            product_width_cm           INT NULL,
            PRIMARY KEY (product_id)
        )""", 32_951),
    ("orders", "olist_orders_dataset.csv", """
        CREATE TABLE orders (
            order_id                      CHAR(32)    NOT NULL,
            customer_id                   CHAR(32)    NOT NULL,
            order_status                  VARCHAR(16) NOT NULL,
            order_purchase_timestamp      DATETIME    NOT NULL,
            order_approved_at             DATETIME    NULL,
            order_delivered_carrier_date  DATETIME    NULL,
            order_delivered_customer_date DATETIME    NULL,  -- 2,965 个为空，其中 8 个订单状态却是 delivered
            order_estimated_delivery_date DATETIME    NOT NULL,
            PRIMARY KEY (order_id)
        )""", 99_441),
    ("order_items", "olist_order_items_dataset.csv", """
        CREATE TABLE order_items (
            order_id            CHAR(32)      NOT NULL,
            order_item_id       SMALLINT      NOT NULL,      -- 订单内序号；一个订单买 3 件同样的商品就是 3 行
            product_id          CHAR(32)      NOT NULL,
            seller_id           CHAR(32)      NOT NULL,
            shipping_limit_date DATETIME      NOT NULL,
            price               DECIMAL(10,2) NOT NULL,      -- 金额用 DECIMAL，避免浮点误差
            freight_value       DECIMAL(10,2) NOT NULL,
            PRIMARY KEY (order_id, order_item_id)
        )""", 112_650),
    ("order_payments", "olist_order_payments_dataset.csv", """
        CREATE TABLE order_payments (
            order_id             CHAR(32)      NOT NULL,
            payment_sequential   SMALLINT      NOT NULL,
            payment_type         VARCHAR(20)   NOT NULL,
            payment_installments SMALLINT      NOT NULL,
            payment_value        DECIMAL(10,2) NOT NULL,
            PRIMARY KEY (order_id, payment_sequential)
        )""", 103_886),
    ("order_reviews", "olist_order_reviews_dataset.csv", """
        CREATE TABLE order_reviews (
            review_id               CHAR(32)      NOT NULL,
            order_id                CHAR(32)      NOT NULL,
            review_score            TINYINT       NOT NULL,
            review_comment_title    VARCHAR(255)  NULL,
            review_comment_message  VARCHAR(1000) NULL,
            review_creation_date    DATETIME      NOT NULL,
            review_answer_timestamp DATETIME      NOT NULL,
            -- review_id 本身有 814 个重复（同一条评价对应多个订单），所以主键是两列组合
            PRIMARY KEY (review_id, order_id)
        )""", 99_224),
    ("geolocation", "olist_geolocation_dataset.csv", """
        CREATE TABLE geolocation (
            id                          INT          NOT NULL AUTO_INCREMENT,  -- 原表没有唯一键（同一邮编有多行坐标）
            geolocation_zip_code_prefix CHAR(5)      NOT NULL,
            geolocation_lat             DOUBLE       NOT NULL,
            geolocation_lng             DOUBLE       NOT NULL,
            geolocation_city            VARCHAR(64)  NOT NULL,
            geolocation_state           CHAR(2)      NOT NULL,
            PRIMARY KEY (id)
        )""", 1_000_163),
]

# 只给外键列建普通索引；Q2.4 的慢查询优化会单独讨论联合索引。
SECONDARY_INDEXES = [
    "CREATE INDEX idx_orders_customer ON orders (customer_id)",
    "CREATE INDEX idx_items_seller ON order_items (seller_id)",
    "CREATE INDEX idx_items_product ON order_items (product_id)",
    "CREATE INDEX idx_reviews_order ON order_reviews (order_id)",
]


def connection_settings() -> dict:
    """读取连接参数；.env 中的值优先于默认值，环境变量优先于 .env。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MYSQL_PORT", "3306")),
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", "root"),
        "database": os.environ.get("MYSQL_DATABASE", "olist"),
    }


def connect(with_database: bool = True) -> pymysql.connections.Connection:
    settings = connection_settings()
    database = settings.pop("database")
    try:
        conn = pymysql.connect(**settings, charset="utf8mb4", autocommit=False,
                               database=database if with_database else None)
    except pymysql.err.OperationalError as exc:
        raise SystemExit(
            f"无法连接 MySQL（{settings['user']}@{settings['host']}:{settings['port']}）：{exc}\n"
            "请先启动 MySQL 8.0，例如：docker run -d --name mysql8 -e MYSQL_ROOT_PASSWORD=root -p 3306:3306 mysql:8\n"
            "连接参数可用环境变量 MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE 修改。"
        ) from exc
    with conn.cursor() as cur:
        cur.execute("SELECT VERSION()")
        version = cur.fetchone()[0]
    if int(version.split(".")[0]) < 8:
        raise SystemExit(f"需要 MySQL 8.0+（窗口函数、CTE），当前版本 {version}")
    return conn


def table_counts(conn) -> dict[str, int]:
    counts = {}
    with conn.cursor() as cur:
        for name, *_ in TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) FROM `{name}`")
                counts[name] = cur.fetchone()[0]
            except pymysql.err.ProgrammingError:
                counts[name] = None
    return counts


def read_csv(path: Path) -> pd.DataFrame:
    """全部按字符读入，空值转成 None；类型转换交给 MySQL 按建表语句完成。"""
    # utf-8-sig：类别翻译表的表头带 BOM，否则第一列列名会变成 '﻿product_category_name'。
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
    # 商品尺寸等列原文是 "40.0" 这样的浮点写法，转成整数字符串再交给 INT 列。
    for column in PRODUCT_INT_COLUMNS & set(frame.columns):
        frame[column] = pd.to_numeric(frame[column]).astype("Int64").astype("string")
    return frame.astype(object).where(frame.notna(), None)


def load_table(conn, name: str, csv_name: str, ddl: str) -> int:
    frame = read_csv(OLIST_DIR / csv_name)
    columns = list(frame.columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = f"INSERT INTO `{name}` ({', '.join(f'`{c}`' for c in columns)}) VALUES ({placeholders})"
    rows = frame.to_numpy().tolist()
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{name}`")
        cur.execute(ddl)
        for start in range(0, len(rows), CHUNK):
            cur.executemany(sql, rows[start:start + CHUNK])
    conn.commit()
    return len(rows)


def ensure_loaded(force: bool = False) -> None:
    """表不存在或行数不对时导入；全部正确时跳过。"""
    missing = [f for _, f, *_ in TABLES if not (OLIST_DIR / f).is_file()]
    if missing:
        raise SystemExit(f"缺少数据文件：{missing}（请先运行 python run_all.py --download）")

    settings = connection_settings()
    server = connect(with_database=False)
    with server.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{settings['database']}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    server.close()

    conn = connect()
    counts = table_counts(conn)
    expected = {name: rows for name, _, _, rows in TABLES}
    if not force and all(counts[n] == expected[n] for n in expected):
        print(f"数据库 {settings['database']} 中 9 张表已完整，跳过导入。")
        conn.close()
        return

    started = time.perf_counter()
    for name, csv_name, ddl, rows in TABLES:
        t0 = time.perf_counter()
        loaded = load_table(conn, name, csv_name, ddl)
        print(f"导入 {name}：{loaded:,} 行（{time.perf_counter() - t0:.1f} 秒）")
        if loaded != rows:
            print(f"  注意：期望 {rows:,} 行，实际 {loaded:,} 行（数据版本可能不同）")
    with conn.cursor() as cur:
        for statement in SECONDARY_INDEXES:
            cur.execute(statement)
    conn.commit()
    print(f"导入完成，用时 {time.perf_counter() - started:.1f} 秒")
    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="把 Olist CSV 导入 MySQL 8.0")
    parser.add_argument("--force", action="store_true", help="删除已有表并重新导入")
    args = parser.parse_args()
    ensure_loaded(force=args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
