"""Q4 抽取流程的单元测试：用假的 API 客户端，不联网、不花钱。"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from q4_llm_extraction import extract_reviews as q4


class FakeClient:
    """按预设剧本返回内容；记录每次收到的批次。"""

    def __init__(self, responder):
        self.stats = q4.Stats()
        self.responder = responder
        self.calls: list[list[dict]] = []

    def call(self, items: list[dict]) -> str:
        self.calls.append(items)
        self.stats.add({"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 50, "completion_tokens": 30},
                       datetime(2026, 11, 28, 12, tzinfo=timezone.utc))  # 周六，非高峰
        return self.responder(items, len(self.calls))


def good_result(item: dict) -> dict:
    return {"id": item["id"], "issues": [{"category": "delivery_late", "severity": "high",
                                          "evidence_quote": item["text"][:10], "evidence_quote_en": "x"}],
            "sentiment_score": -0.8, "actionable_for_seller": True, "confidence": 0.9}


class RuleTests(unittest.TestCase):
    def test_normalize_removes_accents_and_punctuation(self) -> None:
        self.assertEqual(q4.normalize("Não  recebi o PRODUTO!!"), "nao recebi o produto")

    def test_rule_not_received_quotes_original(self) -> None:
        result = q4.apply_rule("nao recebi o produto", "Não recebi o produto")
        self.assertEqual(result["issues"][0]["category"], "delivery_late")
        self.assertEqual(result["issues"][0]["evidence_quote"], "Não recebi o produto")

    def test_rule_positive_short_text_has_no_issue(self) -> None:
        self.assertEqual(q4.apply_rule("bom", "Bom")["issues"], [])

    def test_unknown_text_goes_to_model(self) -> None:
        self.assertIsNone(q4.apply_rule("chegou quebrado e a loja nao responde", "x"))


class ValidationTests(unittest.TestCase):
    def test_ungrounded_and_invalid_issues_are_dropped(self) -> None:
        stats = q4.Stats()
        text = "O produto chegou quebrado"
        raw = {"issues": [
            {"category": "quality_defect", "severity": "high", "evidence_quote": "chegou quebrado", "evidence_quote_en": "arrived broken"},
            {"category": "packaging", "severity": "high", "evidence_quote": "caixa amassada", "evidence_quote_en": "dented box"},  # 原文没有
            {"category": "bad_category", "severity": "high", "evidence_quote": "produto", "evidence_quote_en": "product"},
        ], "sentiment_score": -3, "actionable_for_seller": True, "confidence": 0.95}
        result = q4.validate_result(raw, text, stats)
        self.assertEqual([i["category"] for i in result["issues"]], ["quality_defect"])
        self.assertEqual((stats.dropped_ungrounded, stats.dropped_invalid), (1, 1))
        self.assertEqual(result["sentiment_score"], -1.0)       # 越界值被截断
        self.assertLessEqual(result["confidence"], 0.5)         # 有 issue 被丢弃，置信度下调

    def test_quote_matching_ignores_case_and_accents(self) -> None:
        self.assertTrue(q4.grounded("nao recebi", "Ainda NÃO recebi"))

    def test_parse_batch_handles_bad_json_and_unknown_ids(self) -> None:
        stats = q4.Stats()
        batch = [{"id": "1", "text": "abc def"}]
        self.assertEqual(q4.parse_batch("   ", batch, stats), {})
        content = json.dumps({"results": [{"id": "9", "issues": []}, {"id": "1", "issues": [], "sentiment_score": 0}]})
        self.assertEqual(list(q4.parse_batch(content, batch, stats)), ["1"])


class RunLlmTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(q4, "CACHE_PATH", Path(temp.name) / "cache.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_ids_are_retried_and_cached(self) -> None:
        def responder(items, call_number):
            # 第一次只返回第 1 条，模拟模型漏掉部分 id；之后正常返回。
            chosen = items[:1] if call_number == 1 else items
            return json.dumps({"results": [good_result(i) for i in chosen]})

        texts = ["primeiro texto longo", "segundo texto longo", "terceiro texto longo"]
        client = FakeClient(responder)
        cache = q4.run_llm(texts, client, {}, batch_size=3, workers=1, budget=1.0, started=time.perf_counter())
        self.assertEqual(len(cache), 3)
        self.assertGreater(len(client.calls), 1)
        self.assertEqual(len(q4.CACHE_PATH.read_text(encoding="utf-8").splitlines()), 3)

    def test_cached_texts_are_not_sent_again(self) -> None:
        texts = ["texto um", "texto dois"]
        cache = {q4.text_key("texto um"): {"issues": []}}
        client = FakeClient(lambda items, n: json.dumps({"results": [good_result(i) for i in items]}))
        q4.run_llm(texts, client, cache, batch_size=10, workers=1, budget=1.0, started=time.perf_counter())
        self.assertEqual([i["text"] for i in client.calls[0]], ["texto dois"])

    def test_budget_stops_new_requests(self) -> None:
        client = FakeClient(lambda items, n: json.dumps({"results": [good_result(i) for i in items]}))
        client.stats.cost_usd = 5.0
        q4.run_llm(["a b c", "d e f"], client, {}, batch_size=1, workers=1, budget=1.0, started=time.perf_counter())
        self.assertEqual(client.calls, [])
        self.assertEqual(len(client.stats.failed_texts), 2)


class CostTests(unittest.TestCase):
    def test_peak_hours(self) -> None:
        self.assertTrue(q4.is_peak(datetime(2026, 11, 26, 2, tzinfo=timezone.utc)))      # 周四 UTC 02:00
        self.assertFalse(q4.is_peak(datetime(2026, 11, 26, 13, tzinfo=timezone.utc)))    # 周四 UTC 13:00
        self.assertFalse(q4.is_peak(datetime(2026, 11, 28, 2, tzinfo=timezone.utc)))     # 周六

    def test_usage_cost_off_peak(self) -> None:
        usage = {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1_000_000, "completion_tokens": 1_000_000}
        cost = q4.usage_cost(usage, datetime(2026, 11, 28, 2, tzinfo=timezone.utc))
        self.assertAlmostEqual(cost, 0.003 + 0.15 + 0.60)


if __name__ == "__main__":
    unittest.main()
