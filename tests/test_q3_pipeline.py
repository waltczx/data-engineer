"""Q3 退货匹配规则的单元测试：每个测试用几行模拟数据，对应 match_returns() 文档中的一条规则。"""

import unittest

import pandas as pd

from q3_etl_pipeline import pipeline


def make_frame(rows: list[tuple]) -> pd.DataFrame:
    """rows: (invoice_no, stock_code, quantity, unit_price, 'YYYY-MM-DD HH:MM', customer_id)"""
    frame = pd.DataFrame(rows, columns=["invoice_no", "stock_code", "quantity", "unit_price", "invoice_datetime", "customer_id"])
    frame["description"] = "ITEM"
    frame["country"] = "United Kingdom"
    for column in ("invoice_no", "stock_code", "customer_id", "description", "country"):
        frame[column] = frame[column].astype("string")
    frame["invoice_datetime"] = pd.to_datetime(frame["invoice_datetime"])
    return pipeline.classify_records(frame)


def status_of(frame: pd.DataFrame, invoice: str) -> tuple[str, object]:
    result = pipeline.match_returns(frame)
    row = result.loc[frame.index[frame["invoice_no"] == invoice][0]]
    original = row["matched_original_invoice"]
    return row["match_status"], (None if pd.isna(original) else original)


class MatchReturnsTests(unittest.TestCase):
    def test_exact_quantity_picks_most_recent_earlier_sale(self) -> None:
        frame = make_frame([
            ("100", "111", 5, 1.0, "2010-01-01 10:00", "1"),
            ("101", "111", 5, 1.0, "2010-01-05 10:00", "1"),
            ("102", "111", 5, 1.0, "2010-01-20 10:00", "1"),   # 晚于退货，不是候选
            ("C200", "111", -5, 1.0, "2010-01-10 10:00", "1"),
        ])
        self.assertEqual(status_of(frame, "C200"), ("匹配：同数量", "101"))

    def test_two_partial_returns_share_one_sale(self) -> None:
        # 买 10 个，分两次各退 5 个：两次都应匹配到同一笔原单。
        frame = make_frame([
            ("100", "111", 10, 1.0, "2010-01-01 10:00", "1"),
            ("C200", "111", -5, 1.0, "2010-01-05 10:00", "1"),
            ("C201", "111", -5, 1.0, "2010-01-06 10:00", "1"),
        ])
        result = pipeline.match_returns(frame)
        self.assertEqual(result["matched_original_invoice"].dropna().tolist(), ["100", "100"])
        self.assertTrue(result["match_status"].dropna().eq("匹配：部分退货").all())

    def test_capacity_is_not_exceeded(self) -> None:
        # 买 10 个，先退 8 个，再退 8 个：第二次放不进剩余的 2 个，应改选更早的另一笔原单。
        frame = make_frame([
            ("099", "111", 10, 1.0, "2009-12-01 10:00", "1"),
            ("100", "111", 10, 1.0, "2010-01-01 10:00", "1"),
            ("C200", "111", -8, 1.0, "2010-01-05 10:00", "1"),
            ("C201", "111", -8, 1.0, "2010-01-06 10:00", "1"),
        ])
        result = pipeline.match_returns(frame)
        matched = dict(zip(frame.loc[result.index, "invoice_no"], result["matched_original_invoice"]))
        self.assertEqual(matched["C200"], "100")
        self.assertEqual(matched["C201"], "099")

    def test_return_larger_than_any_sale_is_unmatched(self) -> None:
        # 原单只买了 3 个，退 5 个：单笔原单无法解释这次退货。
        frame = make_frame([
            ("100", "111", 3, 1.0, "2010-01-01 10:00", "1"),
            ("C200", "111", -5, 1.0, "2010-01-05 10:00", "1"),
        ])
        self.assertEqual(status_of(frame, "C200"), ("未匹配：退货数量大于任何一笔更早的购买", None))

    def test_same_price_beats_more_recent(self) -> None:
        # 同一代码两种规格（Q1 中 22502 的情况）：按单价选，而不是只看时间最近。
        frame = make_frame([
            ("556442", "22502", 60, 4.95, "2011-06-10 15:22", "15098"),
            ("556444", "22502", 60, 649.50, "2011-06-10 15:28", "15098"),
            ("C556448", "22502", -60, 4.95, "2011-06-10 15:39", "15098"),
        ])
        self.assertEqual(status_of(frame, "C556448"), ("匹配：同数量", "556442"))

    def test_quick_reversal_removed_from_sales(self) -> None:
        frame = make_frame([
            ("581483", "23843", 80995, 2.08, "2011-12-09 09:15", "16446"),
            ("C581484", "23843", -80995, 2.08, "2011-12-09 09:27", "16446"),
            ("581500", "23843", 2, 2.08, "2011-12-09 10:00", "16446"),
        ])
        matches = pipeline.match_returns(frame)
        self.assertTrue(matches["is_quick_reversal"].any())
        facts = pipeline.filter_real_sales(frame, matches)
        self.assertEqual(facts["invoice_no"].tolist(), ["581500"])

    def test_manual_credit_with_equal_amount_reverses_sale(self) -> None:
        frame = make_frame([
            ("556444", "22502", 60, 649.50, "2011-06-10 15:28", "15098"),
            ("C556445", "M", -1, 38970.00, "2011-06-10 15:31", "15098"),
            ("556446", "22502", 1, 649.50, "2011-06-10 15:33", "15098"),
        ])
        matches = pipeline.match_returns(frame)
        self.assertEqual(status_of(frame, "C556445"), ("匹配：手工冲销（金额相同）", "556444"))
        facts = pipeline.filter_real_sales(frame, matches)
        self.assertEqual(facts["invoice_no"].tolist(), ["556446"])

    def test_return_without_customer_is_unmatched(self) -> None:
        frame = make_frame([
            ("100", "111", 5, 1.0, "2010-01-01 10:00", "1"),
            ("C200", "111", -5, 1.0, "2010-01-05 10:00", None),
        ])
        self.assertEqual(status_of(frame, "C200"), ("未匹配：无客户号", None))


class RfmTests(unittest.TestCase):
    def test_rfm_basic_fields(self) -> None:
        frame = make_frame([
            ("100", "111", 2, 5.0, "2010-01-01 10:00", "1"),
            ("101", "222", 1, 3.0, "2010-01-10 10:00", "1"),
            ("102", "111", 1, 5.0, "2010-01-05 10:00", "2"),
            ("C200", "111", -1, 5.0, "2010-01-12 10:00", "1"),
        ])
        matches = pipeline.match_returns(frame)
        facts = pipeline.filter_real_sales(frame, matches)
        returns_log = pipeline.build_returns_log(frame, matches)
        rfm = pipeline.compute_rfm(facts, returns_log, pd.Timestamp("2010-01-12 18:00")).set_index("customer_id")
        self.assertEqual(rfm.loc["1", "frequency"], 2)
        self.assertEqual(rfm.loc["1", "recency_days"], 2)
        self.assertAlmostEqual(rfm.loc["1", "monetary"], 13.0)
        self.assertAlmostEqual(rfm.loc["1", "net_monetary"], 8.0)
        self.assertTrue(rfm.loc["2", "is_one_time_buyer"])


if __name__ == "__main__":
    unittest.main()
