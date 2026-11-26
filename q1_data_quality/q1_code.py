"""Q1 数据质量探索的初版脚本。

运行方式：
    python q1_data_quality/q1_code.py
    python q1_data_quality/q1_code.py --input data/online_retail_ii.csv --top-n 10

运行后生成本目录的 data_quality_report.md（Q1.1）和 findings.md（Q1.2 的数据证据）。
脚本只计算事实；各类特殊记录的业务解释和 Q1.3 清洗策略需人工核验后撰写。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from pandas.api.types import is_numeric_dtype


# 路径参数：默认从项目根目录的 data/ 读取，结果写回本题目录。
# TOP_N 控制高频值/商品展示数量；SAMPLE_N 控制异常记录展示条数。
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "online_retail_ii.csv"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent
TOP_N = 10
SAMPLE_N = 3

# 数据集版本可能使用不同列名。统一别名后，后续统计只需引用右侧标准名。
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


def markdown_cell(value: object) -> str:
    """把一个值安全地放入 Markdown 表格，避免竖线或换行破坏表格。"""
    if pd.isna(value):
        return "(缺失)"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def number(value: float | int) -> str:
    """统一数值展示格式：千分位和 2 位小数。"""
    return f"{value:,.2f}"


def table(headers: list[str], rows: list[list[object]]) -> str:
    """把二维行数据转成 Markdown 表格；headers 的长度应与每行一致。"""
    result = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    result.extend("| " + " | ".join(markdown_cell(cell) for cell in row) + " |" for row in rows)
    return "\n".join(result)


def load_data(csv_path: Path) -> pd.DataFrame:
    """读取 CSV。参数 csv_path 是输入文件路径；不在脚本里写死机器绝对路径。"""
    if not csv_path.is_file():
        raise FileNotFoundError(f"找不到数据文件：{csv_path}")

    # low_memory=False 让 pandas 基于整列推断类型，减少大文件分块推断造成的混合类型。
    # 标识符先保留为字符串，避免发票号/商品码被误当成数字。
    try:
        header = pd.read_csv(csv_path, nrows=0, encoding="utf-8-sig").columns
        encoding = "utf-8-sig"
    except UnicodeDecodeError:
        # 某些 CSV 导出版本不是 UTF-8；仅在解码失败时回退到 Latin-1。
        encoding = "latin-1"
        header = pd.read_csv(csv_path, nrows=0, encoding=encoding).columns
        print("输入文件不是 UTF-8，已按 Latin-1 编码读取。")
    id_columns = {name: "string" for name in ("Invoice", "InvoiceNo", "StockCode", "Customer ID", "CustomerID") if name in header}
    try:
        frame = pd.read_csv(csv_path, dtype=id_columns, low_memory=False, encoding=encoding)
    except UnicodeDecodeError:
        # 表头可能全为 ASCII，但正文仍包含非 UTF-8 字节。
        frame = pd.read_csv(csv_path, dtype=id_columns, low_memory=False, encoding="latin-1")
        print("输入文件正文不是 UTF-8，已按 Latin-1 编码读取。")
    frame = frame.rename(columns=COLUMN_ALIASES)

    required = {"invoice_no", "stock_code", "description", "quantity", "unit_price", "invoice_datetime", "customer_id", "country"}
    absent = required - set(frame.columns)
    if absent:
        raise ValueError(f"数据缺少必要列：{', '.join(sorted(absent))}")
    if frame.columns.duplicated().any():
        raise ValueError("列名归一化后出现重复列，请先检查原始 CSV 表头")

    # 分析使用独立数值列；原始列仍保留，方便报告展示原始数据类型。
    frame["_quantity_num"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame["_price_num"] = pd.to_numeric(frame["unit_price"], errors="coerce")
    frame["_amount"] = frame["_quantity_num"] * frame["_price_num"]
    return frame


def top_values(series: pd.Series, limit: int) -> list[list[object]]:
    """返回前 limit 个高频值；dropna=False 让缺失值也被统计。"""
    counts = series.astype("string").fillna("(缺失)").value_counts(dropna=False).head(limit)
    return [[value, int(count)] for value, count in counts.items()]


def build_quality_report(frame: pd.DataFrame, top_n: int) -> str:
    """生成 Q1.1：列概况、数值分布、字符串高频值与异常核查项。"""
    visible = frame.loc[:, [name for name in frame.columns if not name.startswith("_")]]
    total = len(visible)
    lines = [
        "# Q1.1 数据质量初探",
        "",
        f"总行数：{total:,}；总列数：{visible.shape[1]}。本报告由 q1_code.py 生成，统计口径为原始交易行，未预先删除退货或异常记录。",
        "",
        "## 各列类型与缺失率",
        "",
    ]
    column_rows = [
        [name, str(visible[name].dtype), int(visible[name].isna().sum()), f"{visible[name].isna().mean():.2%}"]
        for name in visible.columns
    ]
    lines.extend([table(["列名", "读取后类型", "缺失行数", "缺失率"], column_rows), "", "## 数值列分布", ""])

    numeric_rows = []
    for name in visible.columns:
        if is_numeric_dtype(visible[name]):
            values = pd.to_numeric(visible[name], errors="coerce")
            numeric_rows.append([
                name,
                number(values.min()) if values.notna().any() else "(无有效值)",
                number(values.max()) if values.notna().any() else "(无有效值)",
                number(values.mean()) if values.notna().any() else "(无有效值)",
                number(values.std()) if values.notna().sum() > 1 else "(不足 2 行)",
            ])
    lines.append(table(["列名", "min", "max", "mean", "std"], numeric_rows))

    lines.extend(["", f"## 字符串列唯一值与高频值 TOP {top_n}", ""])
    for name in visible.columns:
        if not is_numeric_dtype(visible[name]):
            lines.extend([
                f"### {name}",
                "",
                f"非空唯一值：{visible[name].nunique(dropna=True):,}",
                "",
                table(["值", "行数"], top_values(visible[name], top_n)),
                "",
            ])

    qty = frame["_quantity_num"]
    price = frame["_price_num"]
    invoice = frame["invoice_no"].astype("string")
    stock = frame["stock_code"].astype("string")
    customer_missing = frame["customer_id"].isna()
    # 这里只列可复核的现象和数字，不把每项直接判定为“错误”。
    checks = [
        ("负数量记录", int((qty < 0).sum()), "可能是退货/冲销，不能直接删除"),
        ("零数量记录", int((qty == 0).sum()), "检查是否为占位或系统记录"),
        ("负单价记录", int((price < 0).sum()), "检查描述和费用调整类型"),
        ("零单价记录", int((price == 0).sum()), "检查赠品或异常定价"),
        ("缺失客户 ID", int(customer_missing.sum()), "比较行数占比与金额占比"),
        ("非纯数字发票号", int((invoice.notna() & ~invoice.str.fullmatch(r"\d+", na=False)).sum()), "检查取消单或调整单前缀"),
        ("非常规商品码", int((stock.notna() & ~stock.str.fullmatch(r"\d+[A-Za-z]*", na=False)).sum()), "检查运费、手续费等非商品项目"),
        ("缺失商品描述", int(frame["description"].isna().sum()), "检查与取消单或特殊商品码的关联"),
        ("完全重复行", int(visible.duplicated().sum()), "重复行未必是重复交易，需结合 invoice 和商品判断"),
    ]
    lines.extend([
        "## 可疑现象核查清单",
        "",
        "下表提供超过 5 个待解释现象。最终提交前，请结合 Q1.2 样本写出真实业务判断，而不只保留计数。",
        "",
        table(["核查项", "行数", "下一步判断"], [[name, count, note] for name, count, note in checks]),
        "",
    ])
    return "\n".join(lines)


def sample_table(frame: pd.DataFrame, columns: list[str], limit: int) -> str:
    """从候选行取最多 limit 条完整样本，避免把大批原始记录写入报告。"""
    subset = frame.loc[:, columns].head(limit)
    return table(columns, subset.astype(object).where(pd.notna(subset), "(缺失)").values.tolist())


def build_findings(frame: pd.DataFrame, top_n: int, sample_n: int) -> str:
    """生成 Q1.2 四个问题的统计证据；业务解释留给人工核验。"""
    total_rows = len(frame)
    invoice = frame["invoice_no"].astype("string")
    stock = frame["stock_code"].astype("string")
    qty = frame["_quantity_num"]
    price = frame["_price_num"]
    amount = frame["_amount"]
    lines = [
        "# Q1.2 数据结构发现：统计初稿",
        "",
        "以下数字由脚本生成；业务含义、保留/删除判断及匿名客户推测仍需结合样本人工补写。",
        "",
        "## 问题 1：非常规 InvoiceNo",
        "",
        "支持代码：`non_numeric = invoice.notna() & ~invoice.str.fullmatch(r\"\\d+\", na=False)`；随后用 `str.extract(r\"^([A-Za-z]+)\")` 提取前缀。",
        "",
    ]

    non_numeric = invoice.notna() & ~invoice.str.fullmatch(r"\d+", na=False)
    prefixes = invoice[non_numeric].str.extract(r"^([A-Za-z]+)", expand=False).fillna("其他非纯数字")
    prefix_rows = []
    for prefix, count in prefixes.value_counts().items():
        mask = non_numeric & prefixes.reindex(frame.index).eq(prefix).fillna(False)
        common_descriptions = ", ".join(
            str(value) for value, _ in top_values(frame.loc[mask, "description"], 3)
        )
        prefix_rows.append([prefix, int(count), int((qty[mask] < 0).sum()), number(amount[mask].sum()), common_descriptions])
    lines.extend([
        table(["前缀", "行数", "负数量行数", "金额合计（含正负）", "高频描述"], prefix_rows),
        "",
    ])
    for prefix in prefixes.value_counts().index:
        mask = non_numeric & prefixes.reindex(frame.index).eq(prefix).fillna(False)
        lines.extend([
            f"前缀 {prefix} 的样本（最多 {sample_n} 行）：",
            "",
            sample_table(frame.loc[mask], ["invoice_no", "stock_code", "description", "quantity", "unit_price"], sample_n),
            "",
        ])
    lines.extend([
        "待补：逐个前缀解释业务含义，以及销售总额统计是否纳入。",
        "",
        "## 问题 2：非常规 StockCode",
        "",
        "支持代码：`special = stock.notna() & ~stock.str.fullmatch(r\"\\d+[A-Za-z]*\", na=False)`；金额按 `Quantity × UnitPrice` 汇总。",
        "",
    ])

    special = stock.notna() & ~stock.str.fullmatch(r"\d+[A-Za-z]*", na=False)
    special_rows = []
    for code, count in stock[special].value_counts().items():
        mask = special & stock.eq(code).fillna(False)
        common_descriptions = ", ".join(
            str(value) for value, _ in top_values(frame.loc[mask, "description"], 3)
        )
        special_rows.append([code, int(count), number(amount[mask].sum()), common_descriptions])
    special_amount = amount[special].sum()
    all_amount = amount.sum()
    lines.extend([
        table(["StockCode", "行数", "金额合计（含正负）", "高频描述"], special_rows),
        "",
        f"非常规 StockCode：{int(special.sum()):,} 行，占全部交易行 {special.mean():.2%}；金额合计 {number(special_amount)}，占全表有符号净金额 {special_amount / all_amount:.2%}。" if all_amount else "全表有符号净金额为 0，无法计算金额占比。",
        "",
        "金额比例采用有符号净额；正负冲销可能互相抵消，解释时请同时看各代码金额。",
        "",
        "待补：逐种代码解释业务含义，并确定下游处理方式。",
        "",
        "## 问题 3：数量极值与负单价",
        "",
        "支持代码：`qty.idxmin()`、`qty.idxmax()` 定位极值行；`price < 0` 筛选负单价记录。",
        "",
    ])

    extreme = frame.loc[list(dict.fromkeys([qty.idxmin(), qty.idxmax()]))] if qty.notna().any() else frame.iloc[:0]
    display_columns = [name for name in frame.columns if not name.startswith("_")]
    lines.extend([
        f"数量最小值：{number(qty.min()) if qty.notna().any() else '(无有效值)'}；最大值：{number(qty.max()) if qty.notna().any() else '(无有效值)'}。",
        "",
        sample_table(extreme, display_columns, 2),
        "",
        f"负单价共 {int((price < 0).sum()):,} 行；高频描述：",
        "",
        table(["描述", "行数"], top_values(frame.loc[price < 0, "description"], top_n)),
        "",
        "负单价对应的 StockCode 高频值：",
        "",
        table(["StockCode", "行数"], top_values(frame.loc[price < 0, "stock_code"], top_n)),
        "",
        f"负单价记录中，Quantity 为负的比例：{(qty[price < 0] < 0).mean():.2%}。" if (price < 0).any() else "没有负单价记录。",
        "",
        "待补：核对两条极值是否互为冲销；根据负单价样本判断保留或删除。",
        "",
        "## 问题 4：缺失 CustomerID 的群体",
        "",
        "支持代码：`missing_id = frame[\"customer_id\"].isna()`；金额占比为 `amount[missing_id].sum() / amount.sum()`。",
        "",
    ])

    missing_id = frame["customer_id"].isna()
    missing_amount = amount[missing_id].sum()
    lines.append(
        f"缺失客户 ID：{int(missing_id.sum()):,}/{total_rows:,} 行（{missing_id.mean():.2%}）；金额合计 {number(missing_amount)}，占全表有符号净金额 {missing_amount / all_amount:.2%}。"
        if all_amount else f"缺失客户 ID：{int(missing_id.sum()):,}/{total_rows:,} 行（{missing_id.mean():.2%}）；全表净金额为 0，金额占比不可计算。"
    )
    lines.extend(["", "客单价此处定义为**每张发票的平均净金额**，因为缺失客户 ID 的记录无法按客户聚合。", ""])
    for label, mask in (("缺失客户 ID", missing_id), ("有客户 ID", ~missing_id)):
        subset = frame.loc[mask]
        invoice_amounts = subset.groupby("invoice_no", dropna=True)["_amount"].sum()
        lines.extend([
            f"### {label}",
            "",
            f"发票数：{len(invoice_amounts):,}；平均每张发票净金额：{number(invoice_amounts.mean()) if len(invoice_amounts) else '(无发票)'}。",
            "",
            "国家 TOP：",
            "",
            table(["国家", "行数"], top_values(subset["country"], top_n)),
            "",
            "商品 TOP（按交易行数）：",
            "",
            table(["StockCode", "行数"], top_values(subset["stock_code"], top_n)),
            "",
        ])
    lines.extend(["待补：结合国家、发票金额和商品差异，推测匿名记录的来源；不要把推测写成事实。", ""])
    return "\n".join(lines)


def save_report(path: Path, content: str, overwrite: bool) -> None:
    """默认保留已存在的报告，避免重跑时抹掉人工补写的业务判断。"""
    if path.exists() and not overwrite:
        print(f"保留已有报告：{path}；如需重新生成，传入 --overwrite")
        return
    path.write_text(content, encoding="utf-8")
    print(f"已生成：{path}")


def main() -> int:
    # 命令行参数：--input 输入 CSV；--output-dir 报告目录；--top-n 高频展示条数；
    # --sample-n 异常样本条数；--overwrite 是否覆盖已有报告。默认值见文件顶部。
    # 这些参数只影响读取和报告输出，原始 CSV 始终不会被修改。
    parser = argparse.ArgumentParser(description="生成 Q1 数据质量报告与发现初稿")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--sample-n", type=int, default=SAMPLE_N)
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有报告；默认保留手写内容")
    args = parser.parse_args()
    if args.top_n < 1 or args.sample_n < 1:
        parser.error("--top-n 和 --sample-n 必须为正整数")

    frame = load_data(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_report(
        args.output_dir / "data_quality_report.md",
        build_quality_report(frame, args.top_n),
        args.overwrite,
    )
    save_report(
        args.output_dir / "findings.md",
        build_findings(frame, args.top_n, args.sample_n),
        args.overwrite,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
