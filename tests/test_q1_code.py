"""Q1 初版的最小验证：只读取测试时创建的 4 行模拟 CSV。"""

import tempfile
import unittest
from pathlib import Path

from q1_data_quality import q1_code


class Q1CodeTests(unittest.TestCase):
    def test_report_contains_key_checks_for_alternate_column_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "sample.csv"
            csv_path.write_text(
                "Invoice,StockCode,Description,Quantity,InvoiceDate,Price,Customer ID,Country\n"
                "536365,85123A,Product,2,2010-01-01,3.0,17850,United Kingdom\n"
                "C536366,POST,Postage,-2,2010-01-02,3.0,,United Kingdom\n"
                "A536367,M,Adjustment,1,2010-01-03,-1.0,,France\n"
                "536368,85123A,Product,0,2010-01-04,0.0,17850,France\n",
                encoding="utf-8",
            )

            frame = q1_code.load_data(csv_path)
            report = q1_code.build_quality_report(frame, top_n=2)
            findings = q1_code.build_findings(frame, top_n=2, sample_n=1)

            self.assertIn("总行数：4", report)
            self.assertIn("缺失客户 ID", report)
            self.assertIn("负单价记录", report)
            self.assertIn("C536366", findings)
            self.assertIn("A536367", findings)
            self.assertIn("POST", findings)
            self.assertIn("M", findings)

    def test_existing_report_is_preserved_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report = Path(temp_dir) / "findings.md"
            report.write_text("人工补写内容", encoding="utf-8")
            q1_code.save_report(report, "自动内容", overwrite=False)
            self.assertEqual(report.read_text(encoding="utf-8"), "人工补写内容")


if __name__ == "__main__":
    unittest.main()
