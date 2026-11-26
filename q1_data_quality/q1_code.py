"""Q1 数据质量探索：生成报告。

运行方式：
    python q1_data_quality/q1_code.py
    python q1_data_quality/q1_code.py --input data/online_retail_ii.csv --top-n 10

每次运行都会重新生成（覆盖）本目录下两份**纯统计**文件：
    data_quality_report.md   Q1.1 数据质量报告
    findings_evidence.md     Q1.2 四个问题及补充发现的数据证据

手写的业务解读放在 findings.md 和 cleaning_strategy.md，本脚本不会读写它们。
探索过程（每条规则是怎么发现的）见 explore.py。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "online_retail_ii.csv"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent
TOP_N = 10      # 高频值展示条数
SAMPLE_N = 3    # 样本记录展示条数

# 数据集不同版本列名不同（Invoice / InvoiceNo 等），统一成右侧的标准名。
COLUMN_ALIASES = {
    "Invoice": "invoice_no",
    "InvoiceNo": "invoice_no",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "Price": "unit_price",
    "UnitPrice": "unit_price",
    "InvoiceDate": "invoice_datetime",
    "Customer ID": "customer_id",
    "CustomerID": "customer_id",
    "Country": "country",
}
REQUIRED = [
    "invoice_no", "stock_code", "description", "quantity",
    "invoice_datetime", "unit_price", "customer_id", "country",
]
# 除数量、单价外都按文本读入：发票号含字母前缀，客户号只是标签，不应参与数值运算。
NUMERIC_SOURCE = {"Quantity", "Price", "UnitPrice"}

# 字母开头、但描述是具体商品的代码族（explore.py 第 8 步：描述如 "DOGS NIGHT COLLAR"、礼品券、儿童画板）。
# 其余字母开头代码的描述是 POSTAGE / Manual / Bank Charges / AMAZON FEE / 测试商品等，判为非商品。
LETTER_PRODUCT_PREFIXES = ("DCGS", "GIFT", "SP")

# 记录类型，按优先级从上到下判定，互斥（一行只属于一类）。
CAT_BAD_DEBT = "坏账调整（A 发票）"
CAT_RETURN = "退货冲销（C 发票）"
CAT_STOCK_ADJ = "库存调整（零价、无客户号）"
CAT_FREE = "零价出货（零价、有客户号）"
CAT_NON_PRODUCT = "非商品代码（运费/手续费/调整/测试）"
CAT_LETTER_PRODUCT = "字母代码商品销售（DCGS/礼品券等）"
CAT_SALE = "普通商品销售"
CAT_OTHER = "其他"
CATEGORY_ORDER = [CAT_SALE, CAT_LETTER_PRODUCT, CAT_RETURN, CAT_NON_PRODUCT,
                  CAT_STOCK_ADJ, CAT_FREE, CAT_BAD_DEBT, CAT_OTHER]
# "商品销售"口径：两类真实商品的正向成交。
SALES_CATEGORIES = [CAT_SALE, CAT_LETTER_PRODUCT]

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# Markdown 小工具
# ---------------------------------------------------------------------------
def markdown_cell(value: object) -> str:
    """把一个值安全地放进 Markdown 表格：处理缺失值、竖线和换行。"""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return "(缺失)"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def number(value: float | int) -> str:
    """千分位 + 2 位小数；空值显示为 (无)。"""
    if pd.isna(value):
        return "(无)"
    return f"{value:,.2f}"


def percent(part: float, whole: float) -> str:
    """百分比；分母为 0 或分子为空时不报错。"""
    if not whole or pd.isna(part):
        return "(无法计算)"
    return f"{part / whole:.2%}"


def table(headers: list[str], rows: list[list[object]]) -> str:
    """把二维数据转成 Markdown 表格。"""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(lines)


def top_values(series: pd.Series, limit: int) -> list[list[object]]:
    """前 limit 个高频值；缺失值也计入，显示为 (缺失)。"""
    # 先按值排序、再按次数稳定排序：次数相同时按值的字母顺序，保证不同 pandas 版本输出一致。
    counts = series.astype("string").fillna("(缺失)").value_counts().sort_index().sort_values(ascending=False, kind="stable").head(limit)
    return [[value, int(count)] for value, count in counts.items()]


def top_text(series: pd.Series, limit: int = 3) -> str:
    """把前几个高频值拼成一句话，用于表格里的"高频描述"列。"""
    return ", ".join(str(value) for value, _ in top_values(series, limit))


def sample_table(frame: pd.DataFrame, columns: list[str], limit: int) -> str:
    """取前 limit 行作为样本展示。"""
    subset = frame.loc[:, columns].head(limit)
    return table(columns, subset.astype(object).values.tolist())


def flag(series: pd.Series) -> pd.Series:
    """把可能含缺失值的布尔列转成纯 True/False（缺失视为 False）。"""
    return series.fillna(False).astype(bool)


# ---------------------------------------------------------------------------
# 读取与预处理
# ---------------------------------------------------------------------------
def load_data(csv_path: Path) -> pd.DataFrame:
    """读取 CSV 并添加分析用的辅助列（以下划线开头，原始 8 列不改含义）。"""
    if not csv_path.is_file():
        raise FileNotFoundError(f"找不到数据文件：{csv_path}")

    # 先只读表头来探测编码：部分导出版本不是 UTF-8。
    encoding = "utf-8-sig"
    try:
        header = pd.read_csv(csv_path, nrows=0, encoding=encoding).columns
    except UnicodeDecodeError:
        encoding = "latin-1"
        header = pd.read_csv(csv_path, nrows=0, encoding=encoding).columns
    text_columns = {name: "string" for name in header if name not in NUMERIC_SOURCE}
    try:
        frame = pd.read_csv(csv_path, dtype=text_columns, encoding=encoding)
    except UnicodeDecodeError:
        # 表头是纯 ASCII、正文才出现非 UTF-8 字节的情况。
        frame = pd.read_csv(csv_path, dtype=text_columns, encoding="latin-1")
    frame = frame.rename(columns=COLUMN_ALIASES)

    absent = set(REQUIRED) - set(frame.columns)
    if absent:
        raise ValueError(f"数据缺少必要列：{', '.join(sorted(absent))}")

    # 原始 CSV 里客户号写成 "13085.0"（被当作浮点数导出）。去掉 ".0" 还原为标签，
    # 并记下有多少行是这种写法，作为一条数据质量发现写进报告。
    float_like = flag(frame["customer_id"].str.endswith(".0"))
    frame.attrs["customer_id_float_like"] = int(float_like.sum())
    frame["customer_id"] = frame["customer_id"].str.replace(r"\.0$", "", regex=True)

    frame["_qty"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame["_price"] = pd.to_numeric(frame["unit_price"], errors="coerce")
    frame["_amount"] = frame["_qty"] * frame["_price"]
    # 日期在原文件中是文本；解析成 datetime 才能做时间维度的核查。
    frame["_dt"] = pd.to_datetime(frame["invoice_datetime"], errors="coerce")
    # 商品代码归一化：去首尾空格、统一大写（explore.py 第 9 步：72349b 与 72349B 描述相同，是同一商品）。
    frame["_stock"] = frame["stock_code"].str.strip().str.upper()
    # "数字代码" = 数字开头 + 可选字母后缀（如 85123A）。
    frame["_numeric_code"] = flag(frame["_stock"].str.fullmatch(r"\d+[A-Z]*"))
    frame["_letter_product"] = ~frame["_numeric_code"] & flag(frame["_stock"].str.startswith(LETTER_PRODUCT_PREFIXES))
    frame["_desc_case"] = description_case(frame["description"])
    frame["_category"] = classify_records(frame)
    return frame


def description_case(text: pd.Series) -> pd.Series:
    """按大小写写法给描述分类（explore.py 第 2 步）。"""
    has_lower = flag(text.str.contains(r"[a-z]"))
    has_upper = flag(text.str.contains(r"[A-Z]"))
    labels = np.select(
        [text.isna().to_numpy(dtype=bool), (has_upper & ~has_lower).to_numpy(),
         (has_lower & ~has_upper).to_numpy(), (has_lower & has_upper).to_numpy()],
        ["缺失", "全大写", "全小写", "大小写混合"],
        default="无字母",
    )
    return pd.Series(labels, index=text.index)


def classify_records(frame: pd.DataFrame) -> pd.Series:
    """给每一行打上互斥的记录类型标签。

    np.select 按条件顺序取第一个满足的，所以顺序就是优先级：
    例如一行既是 C 发票又是 POST 运费，归为"退货冲销"。
    """
    invoice = frame["invoice_no"]
    no_customer = frame["customer_id"].isna()
    positive = (frame["_qty"] > 0) & (frame["_price"] > 0)
    conditions = [
        flag(invoice.str.startswith("A")),
        flag(invoice.str.startswith("C")),
        (frame["_price"] == 0) & no_customer,
        (frame["_price"] == 0) & ~no_customer,
        ~frame["_numeric_code"] & ~frame["_letter_product"],
        frame["_letter_product"] & positive,
        frame["_numeric_code"] & positive,
    ]
    labels = [CAT_BAD_DEBT, CAT_RETURN, CAT_STOCK_ADJ, CAT_FREE, CAT_NON_PRODUCT, CAT_LETTER_PRODUCT, CAT_SALE]
    return pd.Series(
        np.select([c.to_numpy(dtype=bool) for c in conditions], labels, default=CAT_OTHER),
        index=frame.index,
    )


def visible_columns(frame: pd.DataFrame) -> list[str]:
    """原始 8 列（不含下划线开头的辅助列）。"""
    return [name for name in frame.columns if not name.startswith("_")]


# ---------------------------------------------------------------------------
# 各项核查：每个函数只返回数字，不写结论
# ---------------------------------------------------------------------------
def duplicate_summary(frame: pd.DataFrame) -> dict[str, object]:
    """完全重复行的数量，以及文件内是否存在日期回退的拼接点。

    原文件大体按时间排序。若某一行的日期比上一行早 1 天以上，视为两段数据的拼接点；
    回退前后之间的时间区间就是两段数据的重叠期。
    """
    is_dup = frame.duplicated(subset=visible_columns(frame), keep="first")
    result: dict[str, object] = {"is_dup": is_dup, "extra_rows": int(is_dup.sum()), "overlap": None}

    dt = frame["_dt"]
    jumps_back = (dt.diff() < pd.Timedelta(days=-1)).to_numpy(dtype=bool)
    if jumps_back.any():
        position = int(np.argmax(jumps_back))  # 第一处回退的行位置
        start, end = dt.iloc[position], dt.iloc[position - 1]
        in_overlap = (dt >= start) & (dt <= end)
        result["overlap"] = {
            "row_number": position + 2,  # +1 转成从 1 计数，再 +1 算上表头行
            "start": start,
            "end": end,
            "rows": int(in_overlap.sum()),
            "extra_rows": int((is_dup & in_overlap).sum()),
            "extra_rows_outside": int((is_dup & ~in_overlap).sum()),
            "rows_outside": int((~in_overlap).sum()),
        }
    return result


def match_opposite_rows(frame: pd.DataFrame, limit: int) -> list[list[object]]:
    """对数量绝对值最大的几行，查找"同客户、同商品、数量相反"的对冲记录。

    只对 limit 行（默认 6 行）逐行查找，不是全表循环。
    """
    largest = frame.reindex(frame["_qty"].abs().sort_values(ascending=False, kind="stable").index).head(limit)
    rows = []
    for _, row in largest.iterrows():
        same_pair = flag(
            (frame["customer_id"] == row["customer_id"])
            & (frame["_stock"] == row["_stock"])
            & (frame["_qty"] == -row["_qty"])
        )  # 客户号缺失时比较结果是空值，按"不匹配"处理
        opposite = frame[same_pair]
        if len(opposite):
            gap = (opposite["_dt"] - row["_dt"]).abs().min()
            pair = f"{opposite.iloc[0]['invoice_no']}（相隔 {int(gap.total_seconds() // 60)} 分钟）"
        else:
            pair = "未找到"
        rows.append([
            row["invoice_no"], row["stock_code"], row["description"], int(row["_qty"]),
            row["unit_price"], row["invoice_datetime"], row["customer_id"], pair,
        ])
    return rows


def return_matching(frame: pd.DataFrame) -> dict[str, object]:
    """C 发票能否在之前找到"同客户、同商品、同数量"的正向购买，以及间隔多久（explore.py 第 4 步）。

    用去重后的数据，只看有客户号、数字代码的退货行。这里只做统计；正式的退货匹配规则在 Q3 实现。
    """
    unique = frame[~frame.duplicated(subset=visible_columns(frame))]
    returns = unique[(unique["_category"] == CAT_RETURN) & unique["customer_id"].notna() & unique["_numeric_code"]]
    buys = unique[unique["_category"].isin(SALES_CATEGORIES) & unique["customer_id"].notna()]
    result: dict[str, object] = {"returns": len(returns), "matched": 0, "gaps": [], "never_bought": None}
    if returns.empty:
        return result

    left = returns.assign(_abs=-returns["_qty"])[["customer_id", "_stock", "_abs", "_dt"]].reset_index()
    right = buys[["customer_id", "_stock", "_qty", "_dt"]].rename(columns={"_qty": "_abs", "_dt": "_buy_dt"})
    merged = left.merge(right, on=["customer_id", "_stock", "_abs"], how="inner")
    merged = merged[merged["_buy_dt"] <= merged["_dt"]]
    latest = merged.sort_values("_buy_dt", kind="stable").groupby("index").tail(1)
    gap = latest["_dt"] - latest["_buy_dt"]
    bins = [pd.Timedelta(0), pd.Timedelta(hours=1), pd.Timedelta(days=1), pd.Timedelta(days=7),
            pd.Timedelta(days=30), pd.Timedelta(days=365), pd.Timedelta(days=10_000)]
    labels = ["1 小时内", "1 小时 - 1 天", "1 - 7 天", "7 - 30 天", "30 - 365 天", "超过 1 年"]
    counts = pd.cut(gap, bins, labels=labels, include_lowest=True).value_counts().reindex(labels, fill_value=0)
    result["matched"] = len(latest)
    result["gaps"] = [[label, f"{int(count):,}"] for label, count in counts.items()]

    # 按"客户 + 商品"汇总：退的数量是否超过数据期内买的数量。
    returned = returns.groupby(["customer_id", "_stock"])["_qty"].sum().abs()
    bought = buys.groupby(["customer_id", "_stock"])["_qty"].sum()
    pairs = pd.concat([returned.rename("ret"), bought.rename("buy")], axis=1)
    pairs = pairs[pairs["ret"].notna()].fillna({"buy": 0})
    never = pairs.index[pairs["buy"] == 0]
    never_rows = returns.set_index(["customer_id", "_stock"]).loc[never]
    first_month = frame["_dt"].min().to_period("M")
    result["never_bought"] = {
        "pairs": len(pairs),
        "never": len(never),
        "more_than_bought": int((pairs["ret"] > pairs["buy"]).sum()),
        "first_month_rows": int((never_rows["_dt"].dt.to_period("M") == first_month).sum()),
        "rows": len(never_rows),
    }
    return result


def price_analysis(frame: pd.DataFrame) -> dict[str, object]:
    """同一商品的单价差异：多价格比例、批量价、匿名与有客户号的价差、离群高价（explore.py 第 6 步）。"""
    sales = frame[frame["_category"] == CAT_SALE]
    n_prices = sales.groupby("_stock")["_price"].nunique()
    by_group = sales.groupby(["_stock", sales["customer_id"].isna()])["_price"].median().unstack()
    if {True, False} <= set(by_group.columns):
        both = by_group.dropna(subset=[True, False])
        ratio = both[True] / both[False]
    else:
        ratio = pd.Series(dtype=float)

    example_code = sales["_stock"].value_counts().index[0] if len(sales) else None
    example_rows: list[list[object]] = []
    if example_code is not None:
        example = sales[sales["_stock"] == example_code]
        grouped = example.groupby("_price").agg(
            rows=("_qty", "size"), qty_median=("_qty", "median"),
            known=("customer_id", lambda s: s.notna().mean()),
        ).sort_values("rows", ascending=False, kind="stable").head(6)
        example_rows = [[number(price), f"{int(r.rows):,}", number(r.qty_median), f"{r.known:.0%}"]
                        for price, r in grouped.iterrows()]

    # 离群高价：单价超过该商品单价中位数 10 倍的普通商品销售行。
    median_price = sales.groupby("_stock")["_price"].transform("median")
    outliers = sales[sales["_price"] > 10 * median_price].assign(median_price=median_price)
    nan = float("nan")
    return {
        "products": int(len(n_prices)),
        "multi_price_share": float((n_prices > 1).mean()) if len(n_prices) else nan,
        "ratio_products": int(len(ratio)),
        "ratio_median": float(ratio.median()) if len(ratio) else nan,
        "ratio_above_1_5": float((ratio > 1.5).mean()) if len(ratio) else nan,
        "ratio_below_1": float((ratio < 1).mean()) if len(ratio) else nan,
        "example_code": example_code,
        "example_desc": top_text(sales.loc[sales["_stock"] == example_code, "description"], 1) if example_code else "",
        "example_rows": example_rows,
        "outliers": outliers,
        "top_quantities": sales["_qty"].value_counts().head(12),
    }


def channel_markers(frame: pd.DataFrame) -> dict[str, object]:
    """匿名发票与有客户号发票里，DOT（DOTCOM POSTAGE）和 POST 运费行的出现比例（explore.py 第 7 步）。"""
    numeric_invoice = frame[flag(frame["invoice_no"].str.fullmatch(r"\d+"))]
    flags = pd.DataFrame({
        "invoice_no": numeric_invoice["invoice_no"],
        "anonymous": numeric_invoice["customer_id"].isna(),
        "dot": numeric_invoice["_stock"].eq("DOT"),
        "post": numeric_invoice["_stock"].eq("POST"),
    }).groupby("invoice_no").max()
    anonymous, known = flags[flags["anonymous"]], flags[~flags["anonymous"]]
    dot_invoices = flags[flags["dot"]]
    nan = float("nan")
    return {
        "dot_invoices": len(dot_invoices),
        "dot_anonymous_share": float(dot_invoices["anonymous"].mean()) if len(dot_invoices) else nan,
        "anonymous_invoices": len(anonymous),
        "known_invoices": len(known),
        "anonymous_dot": float(anonymous["dot"].mean()) if len(anonymous) else nan,
        "known_dot": float(known["dot"].mean()) if len(known) else nan,
        "anonymous_post": float(anonymous["post"].mean()) if len(anonymous) else nan,
        "known_post": float(known["post"].mean()) if len(known) else nan,
    }


def group_profile(frame: pd.DataFrame, mask: pd.Series) -> dict[str, str]:
    """某一组记录（如"缺客户号"）的画像指标，用于两组对比。"""
    subset = frame.loc[mask]
    sales = subset[subset["_category"].isin(SALES_CATEGORIES)]
    # 客单价口径：每张发票里商品销售行的金额合计。零金额的库存调整发票、运费等不计入。
    invoice_amount = sales.groupby("invoice_no")["_amount"].sum()
    all_invoice_amount = subset.groupby("invoice_no")["_amount"].sum()
    return {
        "行数": f"{len(subset):,}",
        "英国行占比": percent((subset["country"] == "United Kingdom").sum(), len(subset)),
        "商品销售行占比": percent(len(sales), len(subset)),
        "退货冲销行占比": percent((subset["_category"] == CAT_RETURN).sum(), len(subset)),
        "零单价行占比": percent((subset["_price"] == 0).sum(), len(subset)),
        "有商品销售的发票数": f"{len(invoice_amount):,}",
        "发票均额（商品销售）": number(invoice_amount.mean()),
        "发票中位额（商品销售）": number(invoice_amount.median()),
        "发票均额（全部发票，含零金额）": number(all_invoice_amount.mean()),
        "每行数量中位数（商品销售）": number(sales["_qty"].median()),
        "单价中位数（商品销售）": number(sales["_price"].median()),
    }


def misc_checks(frame: pd.DataFrame) -> dict[str, object]:
    """零散的一致性检查（explore.py 第 9、10 步）。"""
    raw = frame["stock_code"].str.strip()
    lower = pd.Series(raw[flag(raw.str.fullmatch(r"\d+[a-z]+"))].unique(), dtype="string")
    twins = lower[lower.str.upper().isin(set(raw.dropna().unique()))]
    countries = frame.groupby("customer_id")["country"].nunique()
    # 按发票分组取时间跨度（分组聚合，非逐行循环）。
    span = frame.groupby("invoice_no")["_dt"].max() - frame.groupby("invoice_no")["_dt"].min()
    return {
        "lower_codes": len(lower),
        "lower_with_upper": len(twins),
        "multi_country_customers": int((countries > 1).sum()),
        "multi_time_invoices": int((span > pd.Timedelta(0)).sum()),
        "max_invoice_span": span.max(),
        "tiny_price_rows": int(((frame["_price"] > 0) & (frame["_price"] < 0.01)).sum()),
        "multi_desc_codes": int((frame.groupby("_stock")["description"].nunique() > 1).sum()),
    }


# ---------------------------------------------------------------------------
# Q1.1 报告
# ---------------------------------------------------------------------------
def build_quality_report(frame: pd.DataFrame, top_n: int) -> str:
    """生成 Q1.1：列概况、数值分布、高频值、记录类型构成、可疑现象。"""
    columns = visible_columns(frame)
    visible = frame.loc[:, columns]
    total = len(frame)
    qty, price, amount, dt = frame["_qty"], frame["_price"], frame["_amount"], frame["_dt"]
    is_c = flag(frame["invoice_no"].str.startswith("C"))
    is_a = flag(frame["invoice_no"].str.startswith("A"))
    missing_customer = frame["customer_id"].isna()
    missing_desc = frame["description"].isna()

    lines = [
        "# Q1.1 数据质量报告",
        "",
        f"本文件由 `q1_code.py` 自动生成，每次运行都会覆盖。总行数：{total:,}；总列数：{len(columns)}。",
        "统计口径为原始交易行，未做任何删除或去重。",
        "",
        "## 1. 各列类型与缺失率",
        "",
        table(
            ["列名", "读取后类型", "缺失行数", "缺失率"],
            [[name, str(visible[name].dtype), int(visible[name].isna().sum()), f"{visible[name].isna().mean():.2%}"]
             for name in columns],
        ),
        "",
        "说明：除 quantity、unit_price 外都按文本读入。invoice_datetime 在原文件中是文本，"
        "分析时另行解析为日期；customer_id 在原文件中写成 `13085.0` 这样的浮点形式，"
        f"共 {frame.attrs.get('customer_id_float_like', 0):,} 行，读取时已去掉 `.0`。",
        "",
        "## 2. 数值列分布",
        "",
        table(
            ["列名", "min", "1% 分位", "中位数", "99% 分位", "max", "mean", "std"],
            [[name, number(s.min()), number(s.quantile(0.01)), number(s.median()),
              number(s.quantile(0.99)), number(s.max()), number(s.mean()), number(s.std())]
             for name, s in (("quantity", qty), ("unit_price", price))],
        ),
        "",
        f"时间范围：{dt.min()} 至 {dt.max()}。",
        "",
        f"## 3. 文本列唯一值与高频值 TOP {top_n}",
        "",
    ]
    for name in columns:
        if name in ("quantity", "unit_price"):
            continue
        lines.extend([
            f"### {name}",
            "",
            f"非空唯一值：{visible[name].nunique(dropna=True):,}",
            "",
            table(["值", "行数"], top_values(visible[name], top_n)),
            "",
        ])

    by_category = frame.groupby("_category").agg(rows=("_amount", "size"), amount=("_amount", "sum"))
    lines.extend([
        "## 4. 记录类型构成",
        "",
        "同一张表里混着多种业务记录。下表按优先级互斥分类，判定顺序：A 发票 → C 发票 → "
        "零单价且无客户号 → 零单价且有客户号 → 非商品代码 → 字母代码商品 → 数字代码商品。"
        "分类依据见 `explore.py` 第 3、8 步。",
        "",
        table(
            ["记录类型", "行数", "行占比", "金额合计（数量×单价）"],
            [[name, f"{int(by_category.loc[name, 'rows']):,}", percent(by_category.loc[name, "rows"], total),
              number(by_category.loc[name, "amount"])]
             for name in CATEGORY_ORDER if name in by_category.index],
        ),
        "",
        f"全表金额净值（含正负）：{number(amount.sum())}。",
        "",
        "## 5. 可疑现象",
        "",
    ])

    negative_not_c = (qty < 0) & ~is_c
    zero_price = price == 0
    dup = duplicate_summary(frame)
    month = dt.dt.to_period("M")
    weekday = dt.dt.day_of_week
    saturday_dates = sorted(dt[weekday == 5].dt.date.unique())
    prices = price_analysis(frame)
    returns = return_matching(frame)
    misc = misc_checks(frame)
    case_in_sales = frame.loc[frame["_category"] == CAT_SALE, "_desc_case"]

    duplicate_text = f"**完全重复行**：8 列全部相同的多余行共 {dup['extra_rows']:,} 行。"
    overlap = dup["overlap"]
    if overlap:
        duplicate_text += (
            f" 文件第 {overlap['row_number']:,} 行处日期从 {overlap['end']} 回退到 {overlap['start']}，"
            f"这段区间内 {overlap['rows']:,} 行中多余重复 {overlap['extra_rows']:,} 行"
            f"（{percent(overlap['extra_rows'], overlap['rows'])}）；区间外重复率仅 "
            f"{percent(overlap['extra_rows_outside'], overlap['rows_outside'])}。"
        )

    suspicious = [
        f"**数量极值正负对称**：quantity 最小 {number(qty.min())}，最大 {number(qty.max())}，"
        f"而中位数只有 {number(qty.median())}、99% 分位为 {number(qty.quantile(0.99))}。",
        f"**负数量不等于 C 发票**：负数量共 {int((qty < 0).sum()):,} 行，其中 C 发票 {int(((qty < 0) & is_c).sum()):,} 行；"
        f"另有 {int(negative_not_c.sum()):,} 行不是 C 发票，其中单价为 0 的 {int((negative_not_c & zero_price).sum()):,} 行、"
        f"缺客户号的 {int((negative_not_c & missing_customer).sum()):,} 行。",
        f"**C 发票大多找不到同数量的原始购买**：有客户号的 C 行（去重后）{returns['returns']:,} 行中，能在之前找到"
        f"同客户、同商品、同数量购买的只有 {returns['matched']:,} 行（{percent(returns['matched'], returns['returns'])}）。",
        f"**负单价**：{int((price < 0).sum()):,} 行，最小 {number(price.min())}；"
        f"其中属于 A 发票的 {int(((price < 0) & is_a).sum()):,} 行。",
        f"**零单价**：{int(zero_price.sum()):,} 行，其中缺客户号 {int((zero_price & missing_customer).sum()):,} 行、"
        f"缺描述 {int((zero_price & missing_desc).sum()):,} 行。",
        f"**缺失描述全部伴随零单价和缺客户号**：缺描述 {int(missing_desc.sum()):,} 行，其中单价为 0 的 "
        f"{int((missing_desc & zero_price).sum()):,} 行、缺客户号的 {int((missing_desc & missing_customer).sum()):,} 行。",
        f"**描述的大小写写法不统一**：普通商品销售行中全大写描述占 "
        f"{percent((case_in_sales == '全大写').sum(), len(case_in_sales))}；全表另有 "
        f"{int((frame['_desc_case'] == '全小写').sum()):,} 行全小写、{int((frame['_desc_case'] == '大小写混合').sum()):,} 行大小写混合。",
        f"**客户号缺失**：{int(missing_customer.sum()):,} 行（{missing_customer.mean():.2%}），"
        f"金额净值占全表 {percent(amount[missing_customer].sum(), amount.sum())}。",
        f"**同一商品单价差异大**：{percent(prices['multi_price_share'], 1)} 的商品不止一个单价；"
        f"两类都卖过的 {prices['ratio_products']:,} 个商品中，匿名记录单价是有客户号记录的 "
        f"{number(prices['ratio_median'])} 倍（中位数）。",
        f"**离群高价**：{len(prices['outliers']):,} 行普通商品销售的单价超过该商品单价中位数的 10 倍；"
        f"另有 {misc['tiny_price_rows']:,} 行单价在 0 到 0.01 之间。",
        duplicate_text,
        f"**周六几乎没有交易**：只有 {len(saturday_dates)} 个周六有记录（{', '.join(str(d) for d in saturday_dates)}），"
        f"共 {int((weekday == 5).sum()):,} 行；周日有 {int((weekday == 6).sum()):,} 行。",
        f"**商品代码不规范**：原始唯一值 {frame['stock_code'].nunique():,} 个，去空格并统一大写后 {frame['_stock'].nunique():,} 个；"
        f"{misc['lower_codes']} 个小写后缀代码中 {misc['lower_with_upper']} 个同时存在大写版本；"
        f"{misc['multi_desc_codes']:,} 个代码对应多于 1 种描述。",
        f"**非数字代码不全是非商品**：{int((~frame['_numeric_code']).sum()):,} 行（{(~frame['_numeric_code']).mean():.2%}）"
        f"的 StockCode 不是\"数字 + 可选字母\"格式，但其中 {int(frame['_letter_product'].sum()):,} 行的描述是具体商品。",
        f"**一个客户号对应多个国家**：{misc['multi_country_customers']} 个客户；"
        f"**一张发票多个时间戳**：{misc['multi_time_invoices']} 张，最大跨度 {misc['max_invoice_span']}。",
        f"**最后一个月不完整**：数据止于 {dt.max()}，{month.max()} 只有 {int((month == month.max()).sum()):,} 行。",
        f"**国家字段含非国家值**：`Unspecified` {int((frame['country'] == 'Unspecified').sum()):,} 行，"
        f"`European Community` {int((frame['country'] == 'European Community').sum()):,} 行。",
    ]
    lines.extend(f"{index}. {text}" for index, text in enumerate(suspicious, start=1))
    lines.extend(["", "各现象的明细证据见 `findings_evidence.md`，业务解读见 `findings.md`。", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Q1.2 证据
# ---------------------------------------------------------------------------
def build_findings_evidence(frame: pd.DataFrame, top_n: int, sample_n: int) -> str:
    """生成 Q1.2 四个问题及补充发现的数据证据（只有数字和样本，不含结论）。"""
    total = len(frame)
    invoice = frame["invoice_no"]
    qty, price, amount, dt = frame["_qty"], frame["_price"], frame["_amount"], frame["_dt"]
    is_c = flag(invoice.str.startswith("C"))
    is_a = flag(invoice.str.startswith("A"))
    total_amount = amount.sum()
    total_abs_amount = amount.abs().sum()
    is_sales = frame["_category"].isin(SALES_CATEGORIES)
    sales_amount = amount[is_sales].sum()
    show = ["invoice_no", "stock_code", "description", "quantity", "unit_price", "invoice_datetime", "customer_id"]

    lines = [
        "# Q1.2 数据证据",
        "",
        "本文件由 `q1_code.py` 自动生成，每次运行都会覆盖；业务解读写在 `findings.md`。",
        "",
        "## 问题 1：非纯数字的 InvoiceNo",
        "",
        "支持代码：`invoice.str.fullmatch(r\"\\d+\")` 判断纯数字，`str.extract(r\"^([A-Za-z]+)\")` 提取前缀。",
        "",
    ]

    # --- 问题 1 ---
    non_numeric = ~flag(invoice.str.fullmatch(r"\d+"))
    prefix = invoice[non_numeric].str.extract(r"^([A-Za-z]+)", expand=False)
    prefix_rows = []
    for name, group in frame[non_numeric].groupby(prefix):
        prefix_rows.append([
            name, f"{len(group):,}", f"{group['invoice_no'].nunique():,}",
            f"{int((group['_qty'] < 0).sum()):,}", f"{int((group['_qty'] > 0).sum()):,}",
            f"{int((group['_price'] < 0).sum()):,}", percent(group["customer_id"].notna().sum(), len(group)),
            number(group["_amount"].sum()), top_text(group["description"]),
        ])
    lines.extend([
        table(["前缀", "行数", "发票数", "负数量行", "正数量行", "负单价行", "有客户号占比", "金额合计", "高频描述"], prefix_rows),
        "",
    ])
    for name in sorted(prefix.dropna().unique()):
        rows = frame[non_numeric][(prefix == name).to_numpy(dtype=bool)]
        lines.extend([f"前缀 {name} 的样本：", "", sample_table(rows, show, 6 if name == "A" else sample_n), ""])

    invoice_type = np.select([is_c.to_numpy(), is_a.to_numpy()], ["C 发票", "A 发票"], default="数字发票")
    qty_sign = np.select([(qty < 0).to_numpy(), (qty == 0).to_numpy()], ["数量<0", "数量=0"], default="数量>0")
    cross = pd.crosstab(invoice_type, qty_sign)
    negative_not_c = frame[(qty < 0) & ~is_c]
    lines.extend([
        "发票类型 × 数量正负（行数）：",
        "",
        table(["发票类型", *cross.columns], [[idx, *[f"{int(v):,}" for v in row]] for idx, row in cross.iterrows()]),
        "",
        f"数字发票但数量为负：{len(negative_not_c):,} 行；其中单价为 0 的 {int((negative_not_c['_price'] == 0).sum()):,} 行，"
        f"缺客户号的 {int(negative_not_c['customer_id'].isna().sum()):,} 行。高频描述：",
        "",
        table(["描述", "行数"], top_values(negative_not_c["description"], top_n)),
        "",
        "C 发票但数量不为负的记录：",
        "",
        sample_table(frame[is_c & (qty >= 0)], show, sample_n),
        "",
    ])

    returns = return_matching(frame)
    lines.extend([
        "### C 发票与之前购买的匹配（去重后，有客户号、数字代码的 C 行）",
        "",
        "支持代码：`return_matching()`，按 客户号 + 商品代码 + |数量| 关联，取 C 行之前最近的一笔购买。",
        "",
        f"C 行 {returns['returns']:,} 行，找到同数量前序购买的 {returns['matched']:,} 行"
        f"（{percent(returns['matched'], returns['returns'])}）。匹配到的行与那笔购买的时间差：",
        "",
        table(["时间差", "行数"], returns["gaps"]),
        "",
    ])
    never = returns["never_bought"]
    if never:
        lines.extend([
            f"按\"客户 + 商品\"汇总：有退货的组合 {never['pairs']:,} 个，其中数据期内从未买过的 {never['never']:,} 个"
            f"（{percent(never['never'], never['pairs'])}），退货总量超过购买总量的 {never['more_than_bought']:,} 个"
            f"（{percent(never['more_than_bought'], never['pairs'])}）。"
            f"\"从未买过\"的组合涉及 {never['rows']:,} 行退货，其中落在数据第一个月的 {never['first_month_rows']:,} 行。",
            "",
        ])

    # --- 问题 2 ---
    special = frame[~frame["_numeric_code"]]
    family = special["_stock"].str.extract(r"^([A-Z_]+)", expand=False).fillna(special["_stock"])
    family_rows = []
    for name, group in sorted(special.groupby(family), key=lambda item: -len(item[1])):
        codes = sorted(group["_stock"].unique())
        family_rows.append([
            name, "商品" if group["_letter_product"].all() else "非商品",
            f"{len(group):,}", len(codes), ", ".join(codes[:4]) + (" …" if len(codes) > 4 else ""),
            percent(group["customer_id"].notna().sum(), len(group)),
            number(group["_amount"].sum()), top_text(group["description"]),
        ])
    non_product_rows = frame[~frame["_numeric_code"] & ~frame["_letter_product"]]
    non_product_category = frame[frame["_category"] == CAT_NON_PRODUCT]
    lines.extend([
        "## 问题 2：非数字格式的 StockCode",
        "",
        "支持代码：`load_data()` 中先 `str.strip().str.upper()` 归一化，再用 `str.fullmatch(r\"\\d+[A-Z]*\")` "
        "判断是否为\"数字 + 可选字母\"格式；不符合的按字母前缀分族，看描述判断是否为商品。",
        "",
        table(["代码族", "判定", "行数", "代码数", "包含代码", "有客户号占比", "金额净值", "高频描述"], family_rows),
        "",
        "判定为非商品的代码占比（含出现在 C/A 发票里的行）：",
        "",
        table(
            ["口径", "非商品代码", "全表", "占比"],
            [
                ["行数", f"{len(non_product_rows):,}", f"{total:,}", percent(len(non_product_rows), total)],
                ["金额净值（正负相抵）", number(non_product_rows["_amount"].sum()), number(total_amount),
                 percent(non_product_rows["_amount"].sum(), total_amount)],
                ["金额绝对值合计（不相抵）", number(non_product_rows["_amount"].abs().sum()), number(total_abs_amount),
                 percent(non_product_rows["_amount"].abs().sum(), total_abs_amount)],
            ],
        ),
        "",
        f"其中不在 C/A 发票里、单价不为 0 的（记录类型\"{CAT_NON_PRODUCT}\"）：{len(non_product_category):,} 行，"
        f"金额 {number(non_product_category['_amount'].sum())}。",
        "",
    ])

    # --- 问题 3 ---
    prices = price_analysis(frame)
    outliers = prices["outliers"].sort_values("_price", ascending=False, kind="stable")
    lines.extend([
        "## 问题 3：极端值",
        "",
        "支持代码：`match_opposite_rows()` 对数量绝对值最大的几行查找\"同客户、同商品、数量相反\"的记录。",
        "",
        table(
            ["invoice_no", "stock_code", "description", "quantity", "unit_price", "invoice_datetime", "customer_id", "对冲记录"],
            match_opposite_rows(frame, 6),
        ),
        "",
        f"负单价记录共 {int((price < 0).sum()):,} 行。A 发票全部记录：",
        "",
        sample_table(frame[is_a], show, 10),
        "",
        "单价最高的记录：",
        "",
        sample_table(frame.sort_values("_price", ascending=False, kind="stable"), show, 5),
        "",
        f"普通商品销售中单价超过该商品中位单价 10 倍的记录：{len(outliers):,} 行（支持代码：`price_analysis()`）。单价最高的 8 行：",
        "",
        table(
            ["invoice_no", "stock_code", "description", "quantity", "unit_price", "该商品中位单价", "customer_id"],
            [[r.invoice_no, r.stock_code, r.description, r.quantity, r.unit_price, number(r.median_price), r.customer_id]
             for r in outliers.head(8).itertuples()],
        ),
        "",
        "单价在 0 到 0.01 之间的记录：",
        "",
        sample_table(frame[(price > 0) & (price < 0.01)], show, sample_n),
        "",
    ])

    # --- 问题 4 ---
    missing = frame["customer_id"].isna()
    channel = channel_markers(frame)
    lines.extend([
        "## 问题 4：缺失 CustomerID",
        "",
        "支持代码：`group_profile()` 分别计算\"缺客户号\"和\"有客户号\"两组的画像指标；`channel_markers()` 统计运费代码分布。",
        "",
        table(
            ["口径", "缺客户号", "全表", "占比"],
            [
                ["行数", f"{int(missing.sum()):,}", f"{total:,}", f"{missing.mean():.2%}"],
                ["金额净值", number(amount[missing].sum()), number(total_amount), percent(amount[missing].sum(), total_amount)],
                ["商品销售金额", number(amount[missing & is_sales].sum()), number(sales_amount),
                 percent(amount[missing & is_sales].sum(), sales_amount)],
            ],
        ),
        "",
    ])
    profile_missing, profile_known = group_profile(frame, missing), group_profile(frame, ~missing)
    lines.extend([
        table(["指标", "缺客户号", "有客户号"], [[key, profile_missing[key], profile_known[key]] for key in profile_missing]),
        "",
        "客单价定义为每张发票里**商品销售行**的金额合计：匿名记录无法按客户聚合，只能按发票；"
        "同时排除零金额的库存调整和运费等行，否则均值会被拉偏（对比上表两种均额口径）。",
        "",
        table(
            ["指标", "缺客户号发票", "有客户号发票"],
            [
                ["发票数（数字发票）", f"{channel['anonymous_invoices']:,}", f"{channel['known_invoices']:,}"],
                ["含 DOT（DOTCOM POSTAGE）行的比例", percent(channel["anonymous_dot"], 1), percent(channel["known_dot"], 1)],
                ["含 POST（POSTAGE）行的比例", percent(channel["anonymous_post"], 1), percent(channel["known_post"], 1)],
            ],
        ),
        "",
        f"含 DOT 行的发票共 {channel['dot_invoices']:,} 张，其中缺客户号的占 {percent(channel['dot_anonymous_share'], 1)}。",
        "",
        f"同一商品的单价：两类都卖过的 {prices['ratio_products']:,} 个商品中，匿名单价 / 有客户号单价的中位数为 "
        f"{number(prices['ratio_median'])}；比值超过 1.5 的占 {percent(prices['ratio_above_1_5'], 1)}，"
        f"低于 1 的占 {percent(prices['ratio_below_1'], 1)}。",
        "",
    ])
    for label, mask in (("缺客户号", missing), ("有客户号", ~missing)):
        subset = frame[mask]
        lines.extend([
            f"### {label}：国家 TOP {top_n}",
            "",
            table(["国家", "行数"], top_values(subset["country"], top_n)),
            "",
            f"### {label}：商品 TOP {top_n}（按行数）",
            "",
            table(
                ["StockCode", "描述", "行数"],
                [[code, top_text(subset.loc[subset["_stock"] == code, "description"], 1), count]
                 for code, count in top_values(subset["_stock"], top_n)],
            ),
            "",
        ])

    # --- 补充 1：描述写法 ---
    present = set(frame["_category"])
    case_table = pd.crosstab(frame["_category"], frame["_desc_case"]).reindex([c for c in CATEGORY_ORDER if c in present])
    lines.extend([
        "## 补充 1：描述的大小写写法 × 记录类型（行数）",
        "",
        "支持代码：`description_case()`。",
        "",
        table(["记录类型", *case_table.columns], [[idx, *[f"{int(v):,}" for v in row]] for idx, row in case_table.iterrows()]),
        "",
    ])
    for name in ("全小写", "大小写混合"):
        lines.extend([
            f"{name}描述的高频值：",
            "",
            table(["描述", "行数"], top_values(frame.loc[frame["_desc_case"] == name, "description"], top_n)),
            "",
        ])

    # --- 补充 2：价格与数量 ---
    lines.extend([
        "## 补充 2：同一商品的单价与数量",
        "",
        f"不止一个单价的商品占 {percent(prices['multi_price_share'], 1)}（共 {prices['products']:,} 个商品）。"
        f"以行数最多的商品 {prices['example_code']}（{prices['example_desc']}）为例：",
        "",
        table(["单价", "行数", "数量中位数", "有客户号占比"], prices["example_rows"]),
        "",
        "普通商品销售中最常见的数量值：",
        "",
        table(["数量", "行数"], [[int(k), f"{int(v):,}"] for k, v in prices["top_quantities"].items()]),
        "",
    ])

    # --- 补充 3：时间维度 ---
    month = dt.dt.to_period("M")
    dup = duplicate_summary(frame)
    monthly = pd.DataFrame({
        "rows": frame.groupby(month).size(),
        "dup": dup["is_dup"].groupby(month).sum(),
        "missing": missing.groupby(month).mean(),
    })
    weekday_counts = dt.dt.day_of_week.value_counts().reindex(range(7), fill_value=0)
    hours = dt.dt.hour.value_counts().sort_index()
    lines.extend([
        "## 补充 3：时间维度",
        "",
        table(["星期", *WEEKDAYS], [["行数", *[f"{int(v):,}" for v in weekday_counts]]]),
        "",
        f"开票时间（小时）范围：{int(hours.index.min())} 点至 {int(hours.index.max())} 点；"
        f"9 点至 16 点（含）开票的行占 {percent(hours[(hours.index >= 9) & (hours.index <= 16)].sum(), hours.sum())}。",
        "",
        table(
            ["月份", "行数", "多余重复行", "重复占比", "缺客户号占比"],
            [[str(idx), f"{int(row.rows):,}", f"{int(row.dup):,}", percent(row.dup, row.rows), f"{row.missing:.1%}"]
             for idx, row in monthly.iterrows()],
        ),
        "",
    ])
    overlap = dup["overlap"]
    if overlap:
        lines.extend([
            f"拼接点：文件第 {overlap['row_number']:,} 行处日期从 {overlap['end']} 回退到 {overlap['start']}。"
            f"该区间共 {overlap['rows']:,} 行，其中多余重复 {overlap['extra_rows']:,} 行"
            f"（{percent(overlap['extra_rows'], overlap['rows'])}）；区间外 {overlap['rows_outside']:,} 行中多余重复 "
            f"{overlap['extra_rows_outside']:,} 行（{percent(overlap['extra_rows_outside'], overlap['rows_outside'])}）。",
            "",
        ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Q1 数据质量报告与证据")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="输入 CSV 路径")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="报告输出目录")
    parser.add_argument("--top-n", type=int, default=TOP_N, help="高频值展示条数")
    parser.add_argument("--sample-n", type=int, default=SAMPLE_N, help="样本记录展示条数")
    args = parser.parse_args()
    if args.top_n < 1 or args.sample_n < 1:
        parser.error("--top-n 和 --sample-n 必须为正整数")

    frame = load_data(args.input)
    print(f"已读取 {len(frame):,} 行：{args.input}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    outputs = {
        "data_quality_report.md": build_quality_report(frame, args.top_n),
        "findings_evidence.md": build_findings_evidence(frame, args.top_n, args.sample_n),
    }
    for name, content in outputs.items():
        (args.output_dir / name).write_text(content, encoding="utf-8")
        print(f"已生成：{args.output_dir / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
