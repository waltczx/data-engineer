"""Q1 探索脚本：第一次看这份数据时用的查询，按"提出问题 -> 查数据"的顺序排列。

用法：
    python q1_data_quality/explore.py            # 依次运行全部步骤
    python q1_data_quality/explore.py 4 6        # 只运行第 4、6 步

这个脚本只打印结果，不生成文件。注释里只写"想回答什么问题"，不写结论：
结论要看运行结果自己判断，整理后写进 findings.md。
正式报告由 q1_code.py 生成；这里的查询是它的来源和草稿。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "online_retail_ii.csv"

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)
pd.set_option("display.max_colwidth", 60)


def load() -> pd.DataFrame:
    """全部按文本读入，先不做任何类型假设。"""
    frame = pd.read_csv(CSV, dtype=str)
    frame["qty"] = pd.to_numeric(frame["Quantity"])
    frame["price"] = pd.to_numeric(frame["Price"])
    frame["dt"] = pd.to_datetime(frame["InvoiceDate"])
    return frame


def shape(series: pd.Series) -> pd.Series:
    """把每个值抽象成"形态"：数字记为 9，大写字母记为 A，小写字母记为 a。"""
    return (
        series.fillna("<缺失>")
        .str.replace(r"[0-9]", "9", regex=True)
        .str.replace(r"[A-Z]", "A", regex=True)
        .str.replace(r"[a-z]", "a", regex=True)
    )


def step1_column_shapes(df: pd.DataFrame) -> None:
    """问题：每一列都有哪些写法？有没有和大多数不一样的？"""
    for column in ["Invoice", "StockCode", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"]:
        shapes = shape(df[column])
        counts = shapes.value_counts()
        print(f"\n[{column}] 共 {len(counts)} 种形态，前 10：")
        for pattern, count in counts.head(10).items():
            examples = df.loc[shapes == pattern, column].dropna().unique()[:3].tolist()
            print(f"  {pattern!r:28} {count:>10,}  例：{examples}")


def step2_description_case(df: pd.DataFrame) -> None:
    """问题：Description 的大小写写法有没有规律？不同写法的描述分别是什么？"""
    text = df["Description"]
    has_lower = text.str.contains(r"[a-z]", na=False)
    has_upper = text.str.contains(r"[A-Z]", na=False)
    case = pd.Series("无字母", index=df.index)
    case[text.isna()] = "缺失"
    case[has_upper & ~has_lower] = "全大写"
    case[has_lower & ~has_upper] = "全小写"
    case[has_lower & has_upper] = "大小写混合"
    print(case.value_counts())
    for name in ["全小写", "大小写混合", "无字母"]:
        print(f"\n{name} 的高频描述：")
        print(text[case == name].value_counts().head(12).to_string())


def step3_invoice_vs_quantity(df: pd.DataFrame) -> None:
    """问题：发票号开头的字母，和数量的正负有什么关系？两者能互相替代吗？"""
    first = df["Invoice"].str[0].where(~df["Invoice"].str[0].str.isdigit(), "数字")
    sign = df["qty"].gt(0).map({True: "正", False: "负或零"})
    print(pd.crosstab(first, sign))
    odd = df[(first == "数字") & (df["qty"] < 0)]
    print(f"\n数字发票但数量为负：{len(odd):,} 行；单价为 0 的 {int((odd['price'] == 0).sum()):,}，"
          f"缺客户号的 {int(odd['Customer ID'].isna().sum()):,}")
    print(odd["Description"].value_counts(dropna=False).head(12).to_string())


def step4_return_matching(df: pd.DataFrame) -> None:
    """问题：C 开头的记录，能不能在它之前找到一笔"同客户、同商品、同数量"的正向记录？隔了多久？"""
    unique = df.drop_duplicates(subset=["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"])
    is_c = unique["Invoice"].str.startswith("C")
    returns = unique[is_c & unique["Customer ID"].notna()].copy()
    returns["abs_qty"] = -returns["qty"]
    buys = unique[~is_c & (unique["qty"] > 0) & unique["Customer ID"].notna()]
    buys = buys[["Customer ID", "StockCode", "qty", "dt"]].rename(columns={"qty": "abs_qty", "dt": "buy_dt"})
    merged = returns.reset_index().merge(buys, on=["Customer ID", "StockCode", "abs_qty"], how="left")
    merged = merged[merged["buy_dt"].isna() | (merged["buy_dt"] <= merged["dt"])]
    latest = merged.sort_values("buy_dt").groupby("index").tail(1).set_index("index")
    gap = (latest["dt"] - latest["buy_dt"]).dropna()
    print(f"有客户号的 C 行：{len(returns):,}；找到同数量前序购买的：{len(gap):,}（{len(gap) / len(returns):.1%}）")
    bins = [pd.Timedelta(0), pd.Timedelta(hours=1), pd.Timedelta(days=1), pd.Timedelta(days=7),
            pd.Timedelta(days=30), pd.Timedelta(days=365), pd.Timedelta(days=800)]
    labels = ["<1小时", "1小时-1天", "1-7天", "7-30天", "30-365天", ">1年"]
    print(pd.cut(gap, bins, labels=labels, include_lowest=True).value_counts().sort_index().to_string())


def step5_time(df: pd.DataFrame) -> None:
    """问题：交易在星期、小时、月份上怎么分布？完全重复的行集中在什么时候？"""
    print(df["dt"].dt.day_name().value_counts().to_string())
    saturday = df[df["dt"].dt.day_of_week == 5]
    print(f"\n周六的行按日期：{saturday['dt'].dt.date.value_counts().to_dict()}")
    print(f"\n小时分布：{df['dt'].dt.hour.value_counts().sort_index().to_dict()}")
    dup = df.duplicated(subset=["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"])
    by_day = pd.DataFrame({"行数": df.groupby(df["dt"].dt.date).size(), "多余重复": dup.groupby(df["dt"].dt.date).sum()})
    print("\n重复行最多的 12 天：")
    print(by_day.sort_values("多余重复", ascending=False).head(12).to_string())
    back = df["dt"].diff() < pd.Timedelta(days=-1)
    if back.any():
        position = int(back.to_numpy().argmax())
        print(f"\n文件内日期回退：第 {position} 行（从 0 计）从 {df['dt'].iloc[position - 1]} 跳回 {df['dt'].iloc[position]}")


def step6_prices(df: pd.DataFrame) -> None:
    """问题：同一个商品是不是只有一个价格？价格和数量、有无客户号有没有关系？"""
    sales = df[df["Invoice"].str.match(r"^\d") & (df["qty"] > 0) & (df["price"] > 0) & df["StockCode"].str.match(r"^\d")]
    n_prices = sales.groupby("StockCode")["price"].nunique()
    print(f"不止一个单价的商品占比：{(n_prices > 1).mean():.1%}")
    top = sales["StockCode"].value_counts().index[0]
    example = sales[sales["StockCode"] == top]
    print(f"\n例：{top} 按单价分组")
    print(example.groupby("price").agg(行数=("qty", "size"), 数量中位数=("qty", "median"),
                                       有客户号占比=("Customer ID", lambda s: round(s.notna().mean(), 2)))
          .sort_values("行数", ascending=False).head(8).to_string())
    by_group = sales.groupby(["StockCode", sales["Customer ID"].isna()])["price"].median().unstack().dropna()
    ratio = by_group[True] / by_group[False]
    print(f"\n两类都卖过的商品 {len(ratio):,} 个：匿名单价 / 有客户号单价 的中位数 {ratio.median():.2f}")
    print(f"\n最常见的数量值：{sales['qty'].value_counts().head(12).to_dict()}")


def step7_anonymous_invoices(df: pd.DataFrame) -> None:
    """问题：没有客户号的发票里有什么别的发票没有的东西？"""
    rows = df[df["Invoice"].str.match(r"^\d")]
    lines = rows.assign(anonymous=rows["Customer ID"].isna())
    share = lines.groupby("anonymous")["StockCode"].value_counts(normalize=True).unstack(0).fillna(0)
    share["差"] = share[True] - share[False]
    print("匿名记录里明显更常见的 StockCode（按行占比之差）：")
    print(share.sort_values("差", ascending=False).head(8).to_string())


def step8_letter_codes(df: pd.DataFrame) -> None:
    """问题：字母开头的 StockCode 都是什么？看描述，它们像不像商品？"""
    letters = df[~df["StockCode"].str.match(r"^\d")]
    family = letters["StockCode"].str.upper().str.extract(r"^([A-Z_]+)")[0]
    summary = letters.groupby(family).agg(
        行数=("Invoice", "size"),
        代码数=("StockCode", "nunique"),
        描述样例=("Description", lambda s: list(s.dropna().unique()[:3])),
        单价中位数=("price", "median"),
        有客户号占比=("Customer ID", lambda s: round(s.notna().mean(), 2)),
    )
    print(summary.sort_values("行数", ascending=False).to_string())


def step9_code_case(df: pd.DataFrame) -> None:
    """问题：小写后缀的代码（如 72349b）和大写版本（72349B）是不是同一个商品？"""
    lower = df.loc[df["StockCode"].str.match(r"^\d+[a-z]+$"), "StockCode"].unique()
    twins = [code for code in lower if (df["StockCode"] == code.upper()).any()]
    print(f"小写后缀代码 {len(lower)} 个，其中存在大写版本的 {len(twins)} 个")
    for code in twins[:5]:
        print(f"  {code}: {df.loc[df['StockCode'] == code, 'Description'].dropna().unique()[:2]}"
              f" | {code.upper()}: {df.loc[df['StockCode'] == code.upper(), 'Description'].dropna().unique()[:2]}")


def step10_misc(df: pd.DataFrame) -> None:
    """问题：其他零散的可疑点——极小单价、零价但有客户号、一客多国、一张发票多个时间。"""
    print("单价在 0 到 0.01 之间：")
    print(df[(df["price"] > 0) & (df["price"] < 0.01)][["Invoice", "StockCode", "Description", "qty", "price", "Customer ID"]].head(5))
    free = df[(df["price"] == 0) & df["Customer ID"].notna()]
    print(f"\n单价为 0 但有客户号：{len(free):,} 行")
    print(free[["Invoice", "StockCode", "Description", "qty", "Customer ID"]].head(5))
    countries = df.groupby("Customer ID")["Country"].nunique()
    print(f"\n对应多个国家的客户号：{int((countries > 1).sum())} 个")
    stamps = df.groupby("Invoice")["dt"].agg(lambda s: s.max() - s.min())
    print(f"一张发票有多个时间戳：{int((stamps > pd.Timedelta(0)).sum())} 张，最大跨度 {stamps.max()}")


STEPS = [step1_column_shapes, step2_description_case, step3_invoice_vs_quantity, step4_return_matching,
         step5_time, step6_prices, step7_anonymous_invoices, step8_letter_codes, step9_code_case, step10_misc]


def main() -> int:
    chosen = [int(arg) for arg in sys.argv[1:]] or list(range(1, len(STEPS) + 1))
    df = load()
    print(f"读取 {len(df):,} 行，列：{list(df.columns[:8])}")
    for number in chosen:
        step = STEPS[number - 1]
        print(f"\n{'=' * 70}\n第 {number} 步：{step.__doc__.strip()}\n{'=' * 70}")
        step(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
