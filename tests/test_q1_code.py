"""Q1 脚本的最小验证：只读取测试时创建的几行模拟 CSV，不依赖真实数据集。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from q1_data_quality import q1_code


SAMPLE_CSV = (
    "Invoice,StockCode,Description,Quantity,InvoiceDate,Price,Customer ID,Country\n"
    "536365,85123A,PRODUCT,2,2010-01-01 10:00:00,3.0,17850.0,United Kingdom\n"     # 0 普通商品销售
    "C536366,POST,POSTAGE,-2,2010-01-02 10:00:00,3.0,,United Kingdom\n"            # 1 C 发票（同时是运费代码）
    "A536367,B,Adjust bad debt,1,2010-01-03 10:00:00,-1.0,,France\n"               # 2 A 发票
    "536368,85123A,,-5,2010-01-04 10:00:00,0.0,,United Kingdom\n"                 # 3 零价、无客户号、负数量
    "536369,m,Manual,1,2010-01-05 10:00:00,9.0,17850.0,France\n"                  # 4 非商品代码（小写）
    "536370,85123a ,PRODUCT,1,2010-01-06 10:00:00,3.0,17850.0,France\n"           # 5 小写 + 尾空格的商品代码
    "536371,DCGS0058,MISO PRETTY GUM,1,2010-01-07 10:00:00,0.85,,United Kingdom\n"  # 6 字母代码商品
    "C536372,85123A,PRODUCT,-2,2010-01-08 10:00:00,3.0,17850.0,United Kingdom\n"  # 7 能匹配到第 0 行的退货
    "536373,22423,CAKESTAND,1,2010-01-09 10:00:00,0.0,17850.0,United Kingdom\n"   # 8 零价、有客户号
)


class Q1CodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.csv_path = Path(self.temp_dir.name) / "sample.csv"
        self.csv_path.write_text(SAMPLE_CSV, encoding="utf-8")
        self.frame = q1_code.load_data(self.csv_path)

    def test_customer_id_drops_float_suffix(self) -> None:
        self.assertEqual(self.frame.loc[0, "customer_id"], "17850")
        self.assertEqual(self.frame.attrs["customer_id_float_like"], 5)

    def test_stock_code_is_normalized_before_judging_format(self) -> None:
        # "85123a " 去空格并转大写后是数字代码；"m" 归一化为 "M"，仍是非数字代码。
        self.assertEqual(self.frame.loc[5, "_stock"], "85123A")
        self.assertTrue(self.frame.loc[5, "_numeric_code"])
        self.assertFalse(self.frame.loc[4, "_numeric_code"])
        self.assertTrue(self.frame.loc[6, "_letter_product"])

    def test_records_are_classified_by_priority(self) -> None:
        self.assertEqual(
            self.frame["_category"].tolist(),
            [
                q1_code.CAT_SALE,
                q1_code.CAT_RETURN,          # 既是 C 发票又是 POST，按优先级归为退货冲销
                q1_code.CAT_BAD_DEBT,
                q1_code.CAT_STOCK_ADJ,       # 负数量、零单价、非 C 发票
                q1_code.CAT_NON_PRODUCT,
                q1_code.CAT_SALE,
                q1_code.CAT_LETTER_PRODUCT,
                q1_code.CAT_RETURN,
                q1_code.CAT_FREE,
            ],
        )

    def test_description_case(self) -> None:
        self.assertEqual(self.frame.loc[0, "_desc_case"], "全大写")
        self.assertEqual(self.frame.loc[3, "_desc_case"], "缺失")
        self.assertEqual(self.frame.loc[4, "_desc_case"], "大小写混合")

    def test_return_matching_finds_earlier_purchase(self) -> None:
        result = q1_code.return_matching(self.frame)
        self.assertEqual(result["returns"], 1)   # 第 1 行没有客户号，不参与
        self.assertEqual(result["matched"], 1)   # 第 7 行匹配到第 0 行

    def test_reports_contain_key_checks(self) -> None:
        report = q1_code.build_quality_report(self.frame, top_n=2)
        evidence = q1_code.build_findings_evidence(self.frame, top_n=2, sample_n=1)
        self.assertIn("总行数：9", report)
        self.assertIn("负数量不等于 C 发票", report)
        self.assertIn("C536366", evidence)
        self.assertIn("A536367", evidence)
        self.assertIn("DCGS", evidence)

    def test_overlap_is_detected_when_dates_jump_back(self) -> None:
        # 把前两行再追加一遍，模拟两段数据拼接后日期回退、区间内出现重复。
        lines = SAMPLE_CSV.splitlines()
        duplicated = "\n".join(lines + lines[1:3]) + "\n"
        path = Path(self.temp_dir.name) / "overlap.csv"
        path.write_text(duplicated, encoding="utf-8")
        summary = q1_code.duplicate_summary(q1_code.load_data(path))
        self.assertEqual(summary["extra_rows"], 2)
        self.assertIsNotNone(summary["overlap"])
        self.assertEqual(summary["overlap"]["extra_rows"], 2)

    def test_main_always_regenerates_reports(self) -> None:
        output_dir = Path(self.temp_dir.name) / "out"
        output_dir.mkdir()
        (output_dir / "data_quality_report.md").write_text("旧内容", encoding="utf-8")
        handwritten = output_dir / "findings.md"
        handwritten.write_text("手写解读", encoding="utf-8")

        argv = ["q1_code.py", "--input", str(self.csv_path), "--output-dir", str(output_dir)]
        with patch.object(sys, "argv", argv):
            self.assertEqual(q1_code.main(), 0)

        self.assertIn("总行数：9", (output_dir / "data_quality_report.md").read_text(encoding="utf-8"))
        self.assertTrue((output_dir / "findings_evidence.md").is_file())
        # 手写文件不受影响。
        self.assertEqual(handwritten.read_text(encoding="utf-8"), "手写解读")


if __name__ == "__main__":
    unittest.main()
