import pandas as pd
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
file_path = project_root / "data" / "online_retail_ii.csv"
# 按你的实际表头填写

invoice_col = "Invoice"
stock_col = "StockCode"
quantity_col = "Quantity"
price_col = "Price"
numeric_cols = [quantity_col, price_col] # 还有其他数值列，可以继续添加

id_cols = [invoice_col, stock_col]
# 按实际表头填写，截图里的 Customer 可能只是列宽不够
customer_col = "Customer ID"
description_col = "Description"

# 读取所有列，保留原始文本。
# 例如，字符串 "NA" 会保留，是否代表缺失要结合字段含义判断。
df = pd.read_csv(file_path, dtype="string", keep_default_na=False)

# 区分原始空字段和只填了空白字符的字段
stripped = df.apply(lambda col: col.str.strip())
empty = df.eq("")
whitespace_only = df.ne("") & stripped.eq("")
missing = empty | whitespace_only

missing_report = pd.DataFrame({
    "原始空字段": empty.sum(),
    "只有空白字符": whitespace_only.sum(),
    "缺失合计": missing.sum(),
    "缺失率（%）": (missing.mean() * 100).round(2),
})

print("本次读取行数：", len(df))
print("\n全部字段的缺失情况：")
print(missing_report.to_string())

# 后续分析统一把这两种空值记为缺失，df 仍保留读取时的原值
data = stripped.mask(missing)

# 1. 检查发票号和商品代码的重复值、格式和原值示例
patterns = {}

for col in id_cols:
    values = data[col]
    counts = values.value_counts()

    # 数字记成 9、字母记成 A，保留长度和其他符号
    # 比如 00123 → 99999，X123 → A999
    pattern = (
        values.str.replace(r"[0-9]", "9", regex=True)
              .str.replace(r"[A-Za-z]", "A", regex=True)
    )
    patterns[col] = pattern.fillna("空值")

    format_counts = patterns[col].value_counts()
    examples = pd.DataFrame({"格式": patterns[col], "原值示例": df[col]})
    examples = examples.drop_duplicates("格式").set_index("格式")

    print(f"\n【{col}】")
    print("非空唯一值数：", values.nunique())
    print("出现次数最多的 10 个重复值：")
    print(counts[counts > 1].head(10))
    print("各格式的频次和示例：")
    print(format_counts.rename("行数").to_frame().join(examples))

# 同一发票、同一商品代码出现多次，列出来供核查
pair_counts = data.groupby(id_cols).size()
print("\n出现多次的「发票号＋商品代码」前 10 组：")
print(pair_counts[pair_counts > 1].nlargest(10))

# 2. 检查数值分布，50% 就是中位数
numbers = data[list(dict.fromkeys(numeric_cols + [quantity_col]))].apply(
    pd.to_numeric, errors="coerce"
)

for col in numbers.columns:
    values = numbers[col]
    invalid = data[col].notna() & values.isna()

    print(f"\n【{col}】")
    print("有内容但无法转成数字的行数：", invalid.sum())
    print(values.describe(percentiles=[.01, .25, .50, .75, .95, .99]))
    print("最常出现的 10 个数值：")
    print(values.value_counts().head(10))

    extreme_rows = values.dropna().abs().nlargest(5).index
    print("绝对值最大的 5 条记录：")
    print(df.loc[extreme_rows, id_cols + [col]])

# 3. 对照发票前缀、商品代码格式和数量正负
invoice = data[invoice_col]
prefix = invoice.str.extract(r"^([A-Za-z]+)", expand=False).fillna("其他格式")
prefix = prefix.mask(invoice.str.fullmatch(r"[0-9]+", na=False), "纯数字")
prefix = prefix.mask(invoice.isna(), "空值")

quantity = numbers[quantity_col]
sign = pd.Series("缺失或无效", index=df.index)
sign.loc[quantity < 0] = "负数"
sign.loc[quantity == 0] = "零"
sign.loc[quantity > 0] = "正数"

print("\n发票前缀 × 数量正负：")
print(pd.crosstab(prefix, sign))

print("\n商品代码格式 × 数量正负：")
print(pd.crosstab(patterns[stock_col], sign))
price = numbers[price_col]

price_sign = pd.Series("缺失或无效", index=df.index)
price_sign.loc[price < 0] = "负单价"
price_sign.loc[price == 0] = "零单价"
price_sign.loc[price > 0] = "正单价"

print("\n数量正负 × 单价正负：")
print(pd.crosstab(sign, price_sign))

for label in ["负单价", "零单价"]:
    rows = price_sign.eq(label)
    print(f"\n{label}：{rows.sum()} 行，以下是最多 5 条样本")
    print(df.loc[rows, [invoice_col, stock_col, quantity_col, price_col]].head(5))

customer_missing = data[customer_col].isna()
description_missing = data[description_col].isna()

print("\n客户号缺失 × 描述缺失（流水行数）：")
print(pd.crosstab(
    customer_missing.map({True: "缺客户号", False: "有客户号"}),
    description_missing.map({True: "缺描述", False: "有描述"}),
))

# 每张发票有多少条明细，其中多少条缺客户号
invoice_check = (
    data.assign(customer_missing=customer_missing)
        .groupby(invoice_col)["customer_missing"]
        .agg(total_rows="size", missing_rows="sum")
)

all_missing = invoice_check["missing_rows"].eq(invoice_check["total_rows"])
partly_missing = (
    invoice_check["missing_rows"].gt(0)
    & invoice_check["missing_rows"].lt(invoice_check["total_rows"])
)

print("\n按发票检查客户号：")
print("整张发票都缺客户号：", all_missing.sum())
print("只有部分明细缺客户号：", partly_missing.sum())
print("整张发票都填写了客户号：", invoice_check["missing_rows"].eq(0).sum())
print("因发票号缺失、未参与上述分组的行：", data[invoice_col].isna().sum())

# 对比缺客户号的流水是否更常出现零单价、负数量
quantity = pd.to_numeric(data[quantity_col], errors="coerce")
price = pd.to_numeric(data[price_col], errors="coerce")

comparison = pd.DataFrame({
    "客户号情况": customer_missing.map({True: "缺客户号", False: "有客户号"}),
    "数量有效": quantity.notna(),
    "负数量": quantity.lt(0).fillna(False),
    "单价有效": price.notna(),
    "零单价": price.eq(0).fillna(False),
}).groupby("客户号情况").sum()

comparison["负数量占比（%）"] = (
    100 * comparison["负数量"] / comparison["数量有效"].where(
        comparison["数量有效"].gt(0)
    )
).round(2)

comparison["零单价占比（%）"] = (
    100 * comparison["零单价"] / comparison["单价有效"].where(
        comparison["单价有效"].gt(0)
    )
).round(2)

print("\n有无客户号的交易特征对比：")
print(comparison.to_string())
