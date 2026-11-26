"""Q3 端到端 ETL Pipeline：把 online_retail_ii.csv 转成 3 个面向不同用途的数据集。

运行方式：
    python q3_etl_pipeline/pipeline.py
    python q3_etl_pipeline/pipeline.py --input data/online_retail_ii.csv

输出（每次运行都会覆盖）：
    outputs/sales_facts.parquet        真实销售明细，BI 报表用
    outputs/customer_features.parquet  客户 RFM 特征，客户分群用
    outputs/returns_log.parquet        全部 C 发票及其原单匹配结果，退货分析用
    pipeline_log.txt                   运行日志
    validation_report.md               各输出的校验统计

清洗规则来自 Q1 的探索结论，每条规则旁的注释写明了依据。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np
import pandas as pd


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_INPUT = ROOT / "data" / "online_retail_ii.csv"
DEFAULT_OUTPUT_DIR = HERE / "outputs"
LOG_PATH = HERE / "pipeline_log.txt"
REPORT_PATH = HERE / "validation_report.md"

# 题目要求固定随机种子。本 pipeline 没有任何随机步骤，结果的确定性靠"稳定排序 + 固定规则"保证；
# 这里仍然固定种子，防止以后加入抽样等步骤时忘记。
SEED = 42

# 原单在多久之内被同数量冲回，算作"撤销"而不是"退货"。
# 依据（Q1 findings_evidence.md）：能同数量匹配的退货中，1 小时内 1,001 行，1 小时到 1 天只有 348 行。
QUICK_REVERSAL_WINDOW = pd.Timedelta(hours=1)

# 字母开头但描述是具体商品的代码族（Q1 explore.py 第 8 步）。
LETTER_PRODUCT_PREFIXES = ("DCGS", "GIFT", "SP")

# 退货匹配的最大迭代轮数；正常数据几轮内即收敛，超过说明规则有问题。
MAX_MATCH_ROUNDS = 100

COLUMN_ALIASES = {
    "Invoice": "invoice_no", "InvoiceNo": "invoice_no",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "Price": "unit_price", "UnitPrice": "unit_price",
    "InvoiceDate": "invoice_datetime",
    "Customer ID": "customer_id", "CustomerID": "customer_id",
    "Country": "country",
}
FACT_COLUMNS = [
    "invoice_no", "stock_code", "description", "quantity", "unit_price",
    "total_amount", "invoice_datetime", "customer_id", "country",
]

log = logging.getLogger("q3_pipeline")


# ---------------------------------------------------------------------------
# 日志与步骤包装
# ---------------------------------------------------------------------------
def setup_logging(log_path: Path) -> None:
    """日志同时写到终端和 pipeline_log.txt；每次运行覆盖旧日志。"""
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    for handler in (logging.FileHandler(log_path, mode="w", encoding="utf-8"), logging.StreamHandler(sys.stdout)):
        handler.setFormatter(formatter)
        log.addHandler(handler)


@contextmanager
def step(name: str) -> Iterator[None]:
    """每个步骤统一记录开始、耗时和异常；异常记录完整堆栈后继续向上抛，由 main 决定退出码。"""
    log.info("开始：%s", name)
    start = time.perf_counter()
    try:
        yield
    except Exception:
        log.exception("失败：%s", name)
        raise
    log.info("完成：%s（%.2f 秒）", name, time.perf_counter() - start)


# ---------------------------------------------------------------------------
# 1. 读取
# ---------------------------------------------------------------------------
def load_data(csv_path: Path) -> pd.DataFrame:
    """读取原始 CSV，统一列名和类型。"""
    if not csv_path.is_file():
        raise FileNotFoundError(f"找不到数据文件：{csv_path}（请先运行 python run_all.py --download）")

    header = pd.read_csv(csv_path, nrows=0).columns
    text_columns = {name: "string" for name in header if name not in ("Quantity", "Price", "UnitPrice")}
    frame = pd.read_csv(csv_path, dtype=text_columns).rename(columns=COLUMN_ALIASES)
    missing = set(FACT_COLUMNS) - {"total_amount"} - set(frame.columns)
    if missing:
        raise ValueError(f"缺少必要列：{sorted(missing)}")
    log.info("读取 %s 行，列：%s", f"{len(frame):,}", list(frame.columns))

    # 客户号在原文件里是 "13085.0" 的浮点写法，还原成标签（Q1 发现）。
    frame["customer_id"] = frame["customer_id"].str.strip().str.replace(r"\.0$", "", regex=True)
    # 商品代码去空格并统一大写：72349b 与 72349B 是同一商品（Q1 explore.py 第 9 步）。
    frame["stock_code"] = frame["stock_code"].str.strip().str.upper()
    frame["invoice_no"] = frame["invoice_no"].str.strip()
    frame["description"] = frame["description"].str.strip()
    frame["country"] = frame["country"].str.strip()
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="raise").astype("int64")
    frame["unit_price"] = pd.to_numeric(frame["unit_price"], errors="raise").astype("float64")
    # 日期只有一种写法（Q1 explore.py 第 1 步），用固定格式解析，解析失败直接报错。
    frame["invoice_datetime"] = pd.to_datetime(frame["invoice_datetime"], format="%Y-%m-%d %H:%M:%S", errors="raise")
    return frame


# ---------------------------------------------------------------------------
# 2. 去重与分类
# ---------------------------------------------------------------------------
def deduplicate(frame: pd.DataFrame) -> pd.DataFrame:
    """删除 8 列完全相同的重复行。

    依据：Q1 发现 34,335 行多余重复，三分之二集中在 2010-12-01 至 12-09 的文件拼接重叠期。
    重叠期外也有约 1% 的重复，无法区分"真的录了两次"和"导出重复"，统一按重复处理。
    """
    columns = [c for c in FACT_COLUMNS if c != "total_amount"]
    deduped = frame.drop_duplicates(subset=columns, keep="first").reset_index(drop=True)
    log.info("去除完全重复行 %s 行，剩余 %s 行", f"{len(frame) - len(deduped):,}", f"{len(deduped):,}")
    return deduped


def classify_records(frame: pd.DataFrame) -> pd.DataFrame:
    """给每行加上判断用的布尔列（全部向量化）。"""
    frame = frame.copy()
    invoice = frame["invoice_no"]
    code = frame["stock_code"]
    frame["is_return"] = invoice.str.startswith("C").fillna(False).astype(bool)
    frame["is_bad_debt"] = invoice.str.startswith("A").fillna(False).astype(bool)
    numeric_code = code.str.fullmatch(r"\d+[A-Z]*").fillna(False).astype(bool)
    letter_product = code.str.startswith(LETTER_PRODUCT_PREFIXES).fillna(False).astype(bool) & ~numeric_code
    frame["is_product"] = numeric_code | letter_product
    frame["is_sale_line"] = (
        invoice.str.fullmatch(r"\d+").fillna(False).astype(bool)
        & frame["is_product"]
        & (frame["quantity"] > 0)
        & (frame["unit_price"] > 0)  # 零价行是库存调整或零价出货，不是销售（Q1 问题 1）
    )
    frame["total_amount"] = frame["quantity"] * frame["unit_price"]
    counts = {
        "C 发票": int(frame["is_return"].sum()),
        "A 发票": int(frame["is_bad_debt"].sum()),
        "非商品代码": int((~frame["is_product"]).sum()),
        "候选销售行": int(frame["is_sale_line"].sum()),
    }
    log.info("记录分类：%s", counts)
    return frame


# ---------------------------------------------------------------------------
# 3. 退货匹配
# ---------------------------------------------------------------------------
def match_returns(frame: pd.DataFrame) -> pd.DataFrame:
    """为每条 C 发票行寻找原始销售，返回与 C 行同索引的匹配结果。

    规则（依据见 REFLECTION.md 与 Q1 findings.md）：
    1. 候选原单：同客户、同商品、时间不晚于退货、且原单数量 >= 退货数量。
       原单数量比退货少的不能单独解释这次退货，不作为候选。
    2. 多个候选时依次比较：单价是否相同 → 数量是否相同 → 离退货时间是否最近。
       单价放在第一位：只有一个候选原单的退货里，96% 与原单单价相同（数据检查见 REFLECTION.md）；
       同一商品代码下可能有不同规格（如 22502 既有 SMALL 也有 60 PIECES），单价能区分它们。
       单价不是硬性条件，因为约 4% 的真实退货单价与原单不同（推测是折扣或调价）。
    3. 一笔原单可以被多次部分退货，但累计退货数量不能超过原单数量。
       按退货时间先后分配；超额的退货改选下一个候选。这一步按轮迭代，每轮全部向量化。
    4. 手工冲销：代码为 M（Manual）的 C 行，若金额恰好等于同一客户 1 小时内的某一笔销售行金额，
       视为冲掉那笔销售（Q1 问题 3：C556445 冲掉 556444 的 38,970）。全表只有 5 行满足，规则很窄。
    5. 其余无客户号、非商品代码、数量不为负的 C 行不做匹配，结果为空，并记录原因。
    """
    returns = frame[frame["is_return"]]
    sales = frame[frame["is_sale_line"] & frame["customer_id"].notna()]
    matchable = returns[returns["customer_id"].notna() & returns["is_product"] & (returns["quantity"] < 0)]

    left = pd.DataFrame({
        "ret_id": matchable.index, "customer_id": matchable["customer_id"].to_numpy(),
        "stock_code": matchable["stock_code"].to_numpy(), "ret_dt": matchable["invoice_datetime"].to_numpy(),
        "ret_qty": (-matchable["quantity"]).to_numpy(), "ret_price": matchable["unit_price"].to_numpy(),
    })
    right = pd.DataFrame({
        "sale_id": sales.index, "customer_id": sales["customer_id"].to_numpy(),
        "stock_code": sales["stock_code"].to_numpy(), "sale_invoice": sales["invoice_no"].to_numpy(),
        "sale_dt": sales["invoice_datetime"].to_numpy(), "sale_qty": sales["quantity"].to_numpy(),
        "sale_price": sales["unit_price"].to_numpy(),
    })
    all_pairs = left.merge(right, on=["customer_id", "stock_code"], how="inner")
    log.info("退货候选：可匹配的 C 行 %s 行，同客户同商品的候选对 %s 个", f"{len(left):,}", f"{len(all_pairs):,}")

    # 未匹配原因要用到的标记：是否有任何购买、是否有更早的购买、更早的购买里是否有数量足够的。
    earlier = all_pairs["sale_dt"] <= all_pairs["ret_dt"]
    covers = earlier & (all_pairs["sale_qty"] >= all_pairs["ret_qty"])
    flags = pd.DataFrame({
        "any_sale": all_pairs.groupby("ret_id").size() > 0,
        "any_earlier": earlier.groupby(all_pairs["ret_id"]).any(),
        "any_cover": covers.groupby(all_pairs["ret_id"]).any(),
    })

    pairs = all_pairs[covers].copy()
    pairs["exact"] = pairs["sale_qty"] == pairs["ret_qty"]
    pairs["same_price"] = np.isclose(pairs["sale_price"], pairs["ret_price"])
    # 每条退货的候选按优先级排好：同单价 → 同数量 → 时间从近到远；sale_id 作为最后的确定性排序键。
    pairs = pairs.sort_values(["ret_id", "same_price", "exact", "sale_dt", "sale_id"],
                              ascending=[True, False, False, False, False], kind="mergesort")

    capacity = pd.Series(right["sale_qty"].to_numpy(), index=right["sale_id"].to_numpy())
    used = pd.Series(0, index=capacity.index, dtype="int64")
    accepted: list[pd.DataFrame] = []
    rounds = 0
    while not pairs.empty:
        rounds += 1
        if rounds > MAX_MATCH_ROUNDS:
            log.warning("退货匹配超过 %d 轮仍未收敛，剩余 %d 条退货按未匹配处理", MAX_MATCH_ROUNDS, pairs["ret_id"].nunique())
            break
        best = pairs.drop_duplicates("ret_id", keep="first")
        best = best.sort_values(["sale_id", "ret_dt", "ret_id"], kind="mergesort")
        cumulative = best.groupby("sale_id")["ret_qty"].cumsum().to_numpy() + used.reindex(best["sale_id"]).to_numpy()
        fits = cumulative <= capacity.reindex(best["sale_id"]).to_numpy()
        ok, rejected = best[fits], best[~fits]
        accepted.append(ok)
        used = used.add(ok.groupby("sale_id")["ret_qty"].sum(), fill_value=0).astype("int64")
        pairs = pairs[~pairs["ret_id"].isin(ok["ret_id"])]
        # 只有当这条退货单独都放不进原单剩余数量时，才删除这个候选对；否则下一轮重试。
        remaining = (capacity - used).reindex(rejected["sale_id"]).to_numpy()
        hopeless = rejected[rejected["ret_qty"].to_numpy() > remaining][["ret_id", "sale_id"]]
        if not hopeless.empty:
            marked = pairs.merge(hopeless.assign(_drop=True), on=["ret_id", "sale_id"], how="left")
            pairs = pairs[marked["_drop"].isna().to_numpy()]
        if ok.empty and hopeless.empty:
            log.warning("退货匹配第 %d 轮没有进展，停止迭代", rounds)
            break
    log.info("退货匹配迭代 %d 轮", rounds)

    matched = pd.concat(accepted) if accepted else pairs.iloc[:0]
    matched = matched.set_index("ret_id")

    result = pd.DataFrame(index=returns.index)
    result["matched_original_invoice"] = matched["sale_invoice"].reindex(result.index).astype("string")
    result["matched_sale_id"] = matched["sale_id"].reindex(result.index)
    gap = matched["ret_dt"] - matched["sale_dt"]
    result["match_gap"] = gap.reindex(result.index)
    exact = matched["exact"].reindex(result.index, fill_value=False).astype(bool)
    same_price = matched["same_price"].reindex(result.index, fill_value=False).astype(bool)
    is_matched = result["matched_original_invoice"].notna()

    # 每行一个状态：匹配方式或未匹配原因。
    f = flags.reindex(result.index, fill_value=False).astype(bool)
    no_customer = returns["customer_id"].isna()
    status = np.select(
        [
            is_matched & exact & same_price,
            is_matched & exact,
            is_matched & same_price,
            is_matched,
            ~returns["is_product"],
            returns["quantity"] >= 0,
            no_customer,
            ~f["any_sale"],
            ~f["any_earlier"],
            ~f["any_cover"],
        ],
        [
            "匹配：同数量",
            "匹配：同数量（单价不同）",
            "匹配：部分退货",
            "匹配：部分退货（单价不同）",
            "未匹配：非商品代码",
            "未匹配：数量不为负",
            "未匹配：无客户号",
            "未匹配：数据期内无该客户该商品的购买",
            "未匹配：该商品的购买都晚于退货",
            "未匹配：退货数量大于任何一笔更早的购买",
        ],
        default="未匹配：可用原单的剩余数量已被其他退货占满",
    )
    result["match_status"] = pd.Series(status, index=result.index, dtype="string")
    # 撤销：同数量、同单价、1 小时内冲回。对应的原单不算真实销售（Q1 问题 3：80,995、74,215 两对）。
    within = (result["match_gap"] <= QUICK_REVERSAL_WINDOW).fillna(False).astype(bool)
    result["is_quick_reversal"] = exact & same_price & within

    # 规则 4：手工冲销按金额匹配。
    manual = returns[(returns["stock_code"] == "M") & returns["customer_id"].notna() & (returns["quantity"] < 0)]
    credits = pd.DataFrame({
        "ret_id": manual.index, "customer_id": manual["customer_id"].to_numpy(),
        "ret_dt": manual["invoice_datetime"].to_numpy(),
        "credit": (-manual["quantity"] * manual["unit_price"]).to_numpy(),
    })
    sale_amounts = right.assign(sale_amount=right["sale_qty"] * right["sale_price"])
    manual_pairs = credits.merge(sale_amounts, on="customer_id", how="inner")
    manual_pairs = manual_pairs[
        np.isclose(manual_pairs["credit"], manual_pairs["sale_amount"])
        & (manual_pairs["sale_dt"] <= manual_pairs["ret_dt"])
        & (manual_pairs["ret_dt"] - manual_pairs["sale_dt"] <= QUICK_REVERSAL_WINDOW)
    ].sort_values(["ret_id", "sale_dt", "sale_id"], ascending=[True, False, False], kind="mergesort")
    # 每条冲销取最近的一笔；一笔销售只能被冲销一次，且不能已被商品退货判为撤销。
    already = set(result.loc[result["is_quick_reversal"], "matched_sale_id"].dropna().astype("int64"))
    manual_pairs = manual_pairs[~manual_pairs["sale_id"].isin(already)]
    manual_pairs = manual_pairs.drop_duplicates("ret_id").drop_duplicates("sale_id").set_index("ret_id")
    if not manual_pairs.empty:
        idx = manual_pairs.index
        result.loc[idx, "matched_original_invoice"] = manual_pairs["sale_invoice"].astype("string")
        result.loc[idx, "matched_sale_id"] = manual_pairs["sale_id"].astype("float64")
        result.loc[idx, "match_gap"] = manual_pairs["ret_dt"] - manual_pairs["sale_dt"]
        result.loc[idx, "match_status"] = "匹配：手工冲销（金额相同）"
        result.loc[idx, "is_quick_reversal"] = True

    summary = result["match_status"].value_counts()
    for name, count in summary.items():
        level = logging.WARNING if str(name).startswith("未匹配") else logging.INFO
        log.log(level, "退货匹配结果 %s：%s 行", name, f"{count:,}")
    unmatched = result[result["match_status"].str.startswith("未匹配")]
    sample = returns.loc[unmatched.index[:5], ["invoice_no", "stock_code", "quantity", "customer_id"]]
    log.warning("未匹配的 C 行共 %s 行（matched_original_invoice 为空），前 5 行示例：\n%s",
                f"{len(unmatched):,}", sample.to_string())
    log.info("其中判定为撤销（同数量、同单价、%s 内冲回）的 %s 行", QUICK_REVERSAL_WINDOW, f"{int(result['is_quick_reversal'].sum()):,}")
    return result


# ---------------------------------------------------------------------------
# 4. 三个输出
# ---------------------------------------------------------------------------
def to_output_types(frame: pd.DataFrame) -> pd.DataFrame:
    """按题目要求统一类型：日期是 datetime、金额是 float、ID 是 string。"""
    out = frame.loc[:, FACT_COLUMNS].copy()
    for column in ("invoice_no", "stock_code", "description", "customer_id", "country"):
        out[column] = out[column].astype("string")
    out["quantity"] = out["quantity"].astype("int64")
    out["unit_price"] = out["unit_price"].astype("float64")
    out["total_amount"] = out["total_amount"].astype("float64")
    out["invoice_datetime"] = out["invoice_datetime"].astype("datetime64[ns]")
    return out


def filter_real_sales(frame: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """输出 1：真实销售。

    排除：C 发票（退货）、A 发票（坏账）、非商品代码（运费、手续费、调整、测试）、零价行，
    以及 1 小时内被同数量冲回的原单（撤销，不是真实成交）。匿名客户的销售保留。
    """
    reversed_sales = matches.loc[matches["is_quick_reversal"], "matched_sale_id"].astype("int64")
    keep = frame["is_sale_line"] & ~frame.index.isin(reversed_sales)
    log.info("销售行 %s 行，其中被撤销的原单 %s 行，保留 %s 行",
             f"{int(frame['is_sale_line'].sum()):,}", f"{len(reversed_sales):,}", f"{int(keep.sum()):,}")
    facts = to_output_types(frame[keep])
    return facts.sort_values(["invoice_datetime", "invoice_no"], kind="mergesort").reset_index(drop=True)


def build_returns_log(frame: pd.DataFrame, matches: pd.DataFrame) -> pd.DataFrame:
    """输出 3：全部 C 发票行，字段同 sales_facts，加原单匹配结果。

    除题目要求的 matched_original_invoice 外，多加两列方便分析：
    match_status（匹配方式或未匹配原因）、is_quick_reversal（是否为 1 小时内的撤销）。
    """
    returns = frame[frame["is_return"]]
    out = to_output_types(returns)
    out["matched_original_invoice"] = pd.array(matches["matched_original_invoice"].to_numpy(), dtype="string")
    out["match_status"] = pd.array(matches["match_status"].to_numpy(), dtype="string")
    out["is_quick_reversal"] = matches["is_quick_reversal"].to_numpy()
    return out.sort_values(["invoice_datetime", "invoice_no"], kind="mergesort").reset_index(drop=True)


def compute_rfm(sales_facts: pd.DataFrame, returns_log: pd.DataFrame, reference: pd.Timestamp) -> pd.DataFrame:
    """输出 2：每个有客户号的客户的 RFM 特征，全部用 groupby 聚合。

    口径：
    - recency_days：数据集最后一天（reference 的日期部分）减最后一次购买的日期，按自然日计。
    - frequency：真实销售中的不重复发票数。
    - monetary：真实销售金额合计（题目口径，未扣退货）。另给出 returns_amount 与 net_monetary。
    - country：客户发票数最多的国家；并列时取最后一次购买所在的国家，仍并列取字母序靠前的。
    """
    known = sales_facts[sales_facts["customer_id"].notna()]
    grouped = known.groupby("customer_id")
    rfm = pd.DataFrame({
        "first_purchase_date": grouped["invoice_datetime"].min(),
        "last_purchase_date": grouped["invoice_datetime"].max(),
        "frequency": grouped["invoice_no"].nunique(),
        "monetary": grouped["total_amount"].sum(),
        "unique_products": grouped["stock_code"].nunique(),
    })
    rfm["recency_days"] = (reference.normalize() - rfm["last_purchase_date"].dt.normalize()).dt.days.astype("int64")

    by_country = known.groupby(["customer_id", "country"]).agg(
        invoices=("invoice_no", "nunique"), last_seen=("invoice_datetime", "max"),
    ).reset_index()
    by_country = by_country.sort_values(["customer_id", "invoices", "last_seen", "country"],
                                        ascending=[True, False, False, True], kind="mergesort")
    rfm["country"] = by_country.drop_duplicates("customer_id").set_index("customer_id")["country"]

    # 退货金额：商品退货中排除撤销（撤销对应的原单已不在销售里，再扣就重复了）。
    product_returns = returns_log[
        returns_log["customer_id"].notna()
        & ~returns_log["is_quick_reversal"]
        & ~returns_log["match_status"].eq("未匹配：非商品代码")
        & (returns_log["quantity"] < 0)
    ]
    rfm["returns_amount"] = (-product_returns.groupby("customer_id")["total_amount"].sum()).reindex(rfm.index).fillna(0.0)
    rfm["net_monetary"] = rfm["monetary"] - rfm["returns_amount"]
    rfm["is_one_time_buyer"] = rfm["frequency"] == 1

    rfm = rfm.reset_index()
    rfm["customer_id"] = rfm["customer_id"].astype("string")
    rfm["country"] = rfm["country"].astype("string")
    columns = ["customer_id", "recency_days", "frequency", "monetary", "first_purchase_date", "last_purchase_date",
               "unique_products", "country", "is_one_time_buyer", "returns_amount", "net_monetary"]
    return rfm[columns].sort_values("customer_id", kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5. 校验
# ---------------------------------------------------------------------------
def validate_output(sales_facts: pd.DataFrame, customers: pd.DataFrame,
                    returns_log: pd.DataFrame) -> list[tuple[str, bool]]:
    """对三个输出的关键字段做断言；任何一条不满足都会抛出 AssertionError。返回各断言结果，供校验报告使用。"""
    checks: list[tuple[str, bool]] = [
        # sales_facts
        ("sales_facts 非空", len(sales_facts) > 0),
        ("sales_facts 字段与题目一致", list(sales_facts.columns) == FACT_COLUMNS),
        ("sales_facts.total_amount > 0", bool((sales_facts["total_amount"] > 0).all())),
        ("sales_facts.quantity > 0", bool((sales_facts["quantity"] > 0).all())),
        ("sales_facts.unit_price > 0", bool((sales_facts["unit_price"] > 0).all())),
        ("sales_facts.total_amount = quantity × unit_price",
         bool(np.allclose(sales_facts["total_amount"], sales_facts["quantity"] * sales_facts["unit_price"]))),
        ("sales_facts 不含 C/A 等非数字发票", bool(sales_facts["invoice_no"].str.fullmatch(r"\d+").all())),
        ("sales_facts 无完全重复行", not sales_facts.duplicated().any()),
        ("sales_facts 关键字段无空值",
         not sales_facts[["invoice_no", "stock_code", "quantity", "unit_price", "invoice_datetime", "country"]].isna().any().any()),
        ("sales_facts.invoice_datetime 是日期类型", pd.api.types.is_datetime64_any_dtype(sales_facts["invoice_datetime"])),
        ("sales_facts ID 列是字符串类型", all(pd.api.types.is_string_dtype(sales_facts[c]) for c in ("invoice_no", "stock_code", "customer_id"))),
        # customer_features
        ("customer_features 每个客户一行", customers["customer_id"].is_unique),
        ("customer_features 无空客户号", not customers["customer_id"].isna().any()),
        ("recency_days >= 0", bool((customers["recency_days"] >= 0).all())),
        ("frequency >= 1", bool((customers["frequency"] >= 1).all())),
        ("monetary > 0", bool((customers["monetary"] > 0).all())),
        ("first_purchase_date <= last_purchase_date",
         bool((customers["first_purchase_date"] <= customers["last_purchase_date"]).all())),
        ("is_one_time_buyer 与 frequency == 1 一致",
         bool((customers["is_one_time_buyer"] == (customers["frequency"] == 1)).all())),
        ("customer_features 客户数 = sales_facts 中有客户号的客户数",
         len(customers) == sales_facts["customer_id"].nunique()),
        ("monetary 合计 = sales_facts 有客户号的金额合计",
         bool(np.isclose(customers["monetary"].sum(), sales_facts.loc[sales_facts["customer_id"].notna(), "total_amount"].sum()))),
        # returns_log
        ("returns_log 只含 C 发票", bool(returns_log["invoice_no"].str.startswith("C").all())),
        # C496350（代码 M、数量 +1）是已知例外：非商品代码的 C 行不要求数量为负（Q1 问题 1）。
        ("returns_log 中除非商品代码外数量都 < 0",
         bool((returns_log.loc[~returns_log["match_status"].isin(["未匹配：非商品代码", "未匹配：数量不为负"]),
                               "quantity"] < 0).all())),
        ("已匹配行都有原单号、未匹配行原单号为空",
         bool((returns_log["match_status"].str.startswith("匹配") == returns_log["matched_original_invoice"].notna()).all())),
        ("returns_log.matched_original_invoice 是字符串类型", pd.api.types.is_string_dtype(returns_log["matched_original_invoice"])),
        ("原单号都是数字发票", bool(returns_log["matched_original_invoice"].dropna().str.fullmatch(r"\d+").all())),
        ("撤销行一定是同数量同单价匹配，或金额相同的手工冲销",
         bool(returns_log.loc[returns_log["is_quick_reversal"], "match_status"]
              .isin(["匹配：同数量", "匹配：手工冲销（金额相同）"]).all())),
    ]
    failed = [name for name, passed in checks if not passed]
    for name, passed in checks:
        log.info("断言 %s：%s", "通过" if passed else "失败", name)
    assert not failed, f"以下断言未通过：{failed}"
    return checks


def check_match_capacity(frame: pd.DataFrame, matches: pd.DataFrame) -> None:
    """断言：每笔原单被分配的退货数量合计不超过原单数量，且原单早于退货（手工冲销按金额匹配，不参与数量检查）。"""
    matched = matches[matches["matched_sale_id"].notna() & frame.loc[matches.index, "is_product"].to_numpy()]
    sale_ids = matched["matched_sale_id"].astype("int64")
    returned = (-frame.loc[matched.index, "quantity"]).groupby(sale_ids.to_numpy()).sum()
    sold = frame.loc[returned.index, "quantity"]
    assert bool((returned <= sold).all()), "存在原单被退货数量超过其购买数量"
    assert bool((frame.loc[matched.index, "invoice_datetime"].to_numpy()
                 >= frame.loc[sale_ids, "invoice_datetime"].to_numpy()).all()), "存在原单晚于退货"
    log.info("断言 通过：每笔原单的累计退货数量不超过购买数量，且原单都早于退货")


# ---------------------------------------------------------------------------
# 6. 写出
# ---------------------------------------------------------------------------
def content_hash(frame: pd.DataFrame) -> str:
    """数据内容的哈希，用来检查两次运行结果是否完全一致（与文件写出细节无关）。"""
    return f"{int(pd.util.hash_pandas_object(frame, index=False).sum()) & 0xFFFFFFFFFFFFFFFF:016x}"


def write_outputs(outputs: dict[str, pd.DataFrame], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, frame in outputs.items():
        path = output_dir / f"{name}.parquet"
        frame.to_parquet(path, index=False, engine="pyarrow")
        log.info("写出 %s：%s 行，内容哈希 %s", path, f"{len(frame):,}", content_hash(frame))


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines += ["| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def profile(frame: pd.DataFrame) -> str:
    """一张表的行数、各列类型、空值率。"""
    rows = [[c, str(frame[c].dtype), f"{frame[c].isna().mean():.2%}"] for c in frame.columns]
    return f"行数：{len(frame):,}；内容哈希：`{content_hash(frame)}`\n\n" + markdown_table(["列", "类型", "空值率"], rows)


def describe(series: pd.Series) -> list[object]:
    q = series.quantile([0, 0.25, 0.5, 0.75, 0.99, 1])
    return [f"{v:,.2f}" for v in q] + [f"{series.mean():,.2f}"]


def write_validation_report(path: Path, funnel: list[list[object]], sales_facts: pd.DataFrame,
                            customers: pd.DataFrame, returns_log: pd.DataFrame, elapsed: float,
                            checks: list[tuple[str, bool]]) -> None:
    """生成 validation_report.md：每个输出的行数、空值率、关键分布，以及断言结果。"""
    quantile_headers = ["字段", "最小", "25%", "中位数", "75%", "99%", "最大", "均值"]
    status = returns_log["match_status"].value_counts()
    gaps = returns_log["match_status"].str.startswith("匹配").sum()
    lines = [
        "# Q3 数据校验报告",
        "",
        f"本文件由 `pipeline.py` 自动生成，每次运行覆盖。本次运行耗时 {elapsed:.1f} 秒。",
        "内容哈希相同即说明两次运行输出的数据完全一致（可重现性检查）。",
        "",
        "## 从原始数据到 sales_facts",
        "",
        markdown_table(["步骤", "行数"], funnel),
        "",
        "## 输出 1：sales_facts",
        "",
        profile(sales_facts),
        "",
        markdown_table(quantile_headers, [["quantity", *describe(sales_facts["quantity"])],
                                          ["unit_price", *describe(sales_facts["unit_price"])],
                                          ["total_amount", *describe(sales_facts["total_amount"])]]),
        "",
        f"金额合计 {sales_facts['total_amount'].sum():,.2f}；发票 {sales_facts['invoice_no'].nunique():,} 张；"
        f"商品 {sales_facts['stock_code'].nunique():,} 个；无客户号的行占 {sales_facts['customer_id'].isna().mean():.2%}；"
        f"时间范围 {sales_facts['invoice_datetime'].min()} 至 {sales_facts['invoice_datetime'].max()}。",
        "",
        "国家 TOP 5（按金额）：",
        "",
        markdown_table(["国家", "金额", "占比"], [
            [c, f"{v:,.2f}", f"{v / sales_facts['total_amount'].sum():.2%}"]
            for c, v in sales_facts.groupby("country")["total_amount"].sum().nlargest(5).items()]),
        "",
        "## 输出 2：customer_features",
        "",
        profile(customers),
        "",
        markdown_table(quantile_headers, [[c, *describe(customers[c].astype("float64"))]
                                          for c in ("recency_days", "frequency", "monetary", "unique_products", "returns_amount", "net_monetary")]),
        "",
        f"客户 {len(customers):,} 个；只买过一次的占 {customers['is_one_time_buyer'].mean():.2%}；"
        f"net_monetary 为负（退货多于数据期内购买）的客户 {int((customers['net_monetary'] < 0).sum()):,} 个。",
        "",
        "## 输出 3：returns_log",
        "",
        profile(returns_log),
        "",
        markdown_table(["match_status", "行数", "占比"],
                       [[k, f"{v:,}", f"{v / len(returns_log):.2%}"] for k, v in status.items()]),
        "",
        f"匹配成功 {gaps:,} 行（{gaps / len(returns_log):.2%}）；其中判定为 1 小时内撤销的 "
        f"{int(returns_log['is_quick_reversal'].sum()):,} 行，对应原单已从 sales_facts 中剔除。",
        "",
        "## 断言结果",
        "",
        markdown_table(["断言", "结果"], [[name, "通过" if ok else "失败"] for name, ok in checks]),
        "",
        "另有断言：每笔原单的累计退货数量不超过购买数量，且原单都早于退货（`check_match_capacity()`）。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("写出 %s", path.relative_to(ROOT))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(input_path: Path, output_dir: Path) -> None:
    np.random.seed(SEED)
    started = time.perf_counter()
    funnel: list[list[object]] = []

    with step("读取原始数据"):
        raw = load_data(input_path)
        funnel.append(["原始行数", f"{len(raw):,}"])
    with step("去除完全重复行"):
        data = deduplicate(raw)
        funnel.append(["去重后", f"{len(data):,}"])
    with step("记录分类"):
        data = classify_records(data)
        funnel.append(["其中候选销售行（数字发票、商品代码、数量与单价为正）", f"{int(data['is_sale_line'].sum()):,}"])
    with step("退货匹配"):
        matches = match_returns(data)
        check_match_capacity(data, matches)
    with step("生成 sales_facts"):
        sales_facts = filter_real_sales(data, matches)
        funnel.append(["剔除 1 小时内被撤销的原单后（sales_facts）", f"{len(sales_facts):,}"])
    with step("生成 returns_log"):
        returns_log = build_returns_log(data, matches)
    with step("计算客户 RFM 特征"):
        reference = data["invoice_datetime"].max()
        log.info("RFM 基准日：%s（数据集最后一天）", reference.normalize().date())
        customers = compute_rfm(sales_facts, returns_log, reference)
    with step("校验输出"):
        checks = validate_output(sales_facts, customers, returns_log)
    with step("写出文件"):
        write_outputs({"sales_facts": sales_facts, "customer_features": customers, "returns_log": returns_log}, output_dir)
        elapsed = time.perf_counter() - started
        write_validation_report(REPORT_PATH, funnel, sales_facts, customers, returns_log, elapsed, checks)
    log.info("全部完成，总耗时 %.1f 秒", time.perf_counter() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description="Q3 ETL pipeline")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="输入 CSV 路径")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="parquet 输出目录")
    args = parser.parse_args()
    setup_logging(LOG_PATH)
    try:
        run(args.input, args.output_dir)
    except Exception:
        log.error("Pipeline 失败，详见上方日志")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
