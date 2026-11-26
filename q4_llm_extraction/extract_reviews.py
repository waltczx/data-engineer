"""Q4 评价文本结构化抽取：对 Olist 评分 <= 3 的评价抽取问题类别、严重程度、原文证据等。

用法：
    python q4_llm_extraction/extract_reviews.py --dry-run          # 不调用 API：看分流结果和成本预估
    python q4_llm_extraction/extract_reviews.py --limit 5          # 只调用模型处理 5 条，检查效果
    python q4_llm_extraction/extract_reviews.py                    # 全量运行
    python q4_llm_extraction/extract_reviews.py --experiment 40    # 单条 vs 批量请求的成本与一致性对比
    python q4_llm_extraction/extract_reviews.py --export-sample 30 # 导出人工标注样本
    python q4_llm_extraction/extract_reviews.py --evaluate         # 用标注结果计算准确率

API key 从仓库根目录 .env 的 DEEPSEEK_API_KEY 读取。没有 key 时（例如评审直接运行 run_all.py），
程序只使用已缓存的模型结果（llm_cache.jsonl，随仓库提交），不会报错，未缓存的评价会在日志中列出。

流程：读取并去重 → 分流（无文本 / 规则 / 模型）→ 相同文本只调用一次模型 → 校验输出（schema + 原文证据）
→ 写出 extracted_issues.json 与 run_stats.json。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REVIEWS_CSV = ROOT / "data" / "olist" / "olist_order_reviews_dataset.csv"
PROMPT_PATH = HERE / "prompt_template.txt"
CACHE_PATH = HERE / "llm_cache.jsonl"
OUTPUT_PATH = HERE / "extracted_issues.json"
STATS_PATH = HERE / "run_stats.json"
EXPERIMENT_PATH = HERE / "batch_experiment.json"
SAMPLE_PATH = HERE / "annotation_sample.csv"
LEDGER_PATH = HERE / "cost_ledger.jsonl"   # 每次实际调用 API 的花费都追加一行，合计即总花费
EVAL_PATH = HERE / "accuracy_results.json"

API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
PROMPT_VERSION = "v1"          # 修改 prompt 后改这个版本号，旧缓存自动失效
BATCH_SIZE = 20
WORKERS = 24
BUDGET_USD = 2.50              # 题目上限 3 美元，留 0.5 美元余量
TIME_LIMIT_SECONDS = 38 * 60   # 题目时长 40 分钟，留 2 分钟余量
SEED = 42

# 价格（美元 / 百万 token），来源：https://api-docs.deepseek.com/quick_start/pricing（2026-11-26 查询）
# 高峰时段 UTC 周一至周五 01:00-04:00、06:00-10:00（北京时间 09:00-12:00、14:00-18:00），其余时间半价。
PRICES = {
    "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
}

CATEGORIES = {"delivery_late", "wrong_product", "quality_defect", "missing_item",
              "seller_communication", "packaging", "other"}
SEVERITIES = {"low", "medium", "high"}

log = logging.getLogger("q4_extract")


# ---------------------------------------------------------------------------
# 规则分流：只收录在数据中出现 >= 3 次、含义明确的短文本（统计见 pipeline_design.md）。
# 匹配方式是"归一化后整句完全相等"，不做关键词包含，宁可少覆盖也不误判。
# ---------------------------------------------------------------------------
def _issue(category: str, severity: str, english: str) -> dict:
    return {"category": category, "severity": severity, "english": english}


NO_ISSUE_POSITIVE = {  # 短句、正面或中性：没有具体问题
    "bom": 0.6, "boa": 0.6, "ok": 0.3, "recomendo": 0.7, "muito bom": 0.8, "bom produto": 0.7, "otimo": 0.8,
    "otima": 0.8, "bom bom": 0.7, "gostei": 0.7, "gostei do produto": 0.7, "produto bom": 0.7, "tudo ok": 0.4,
    "excelente": 0.9, "tudo certo": 0.5, "o produto e bom": 0.6, "muito boa": 0.8, "satisfeito": 0.6,
    "legal": 0.6, "bom muito bom": 0.8, "obrigado": 0.5, "entregue no prazo": 0.5, "entrega rapida": 0.6,
    "regular": 0.0, "razoavel": 0.0, "sem comentarios": 0.0, "sem opiniao": 0.0,
}
NO_ISSUE_NEGATIVE = {  # 负面但没有说出具体问题：不编造类别
    "pessimo": -0.9, "pessima": -0.9, "ruim": -0.7, "muito ruim": -0.8, "nao recomendo": -0.7,
    "nao gostei": -0.6, "nao gostei do produto": -0.6, "pessima loja": -0.9,
}
NOT_RECEIVED = _issue("delivery_late", "high", "I did not receive the product")
DELAYED = _issue("delivery_late", "medium", "delivery delay")
RULE_ISSUES = {
    **{p: NOT_RECEIVED for p in (
        "nao recebi o produto", "nao recebi", "ainda nao recebi", "produto nao entregue", "nao recebi meu produto",
        "meu produto nao chegou", "produto nao foi entregue", "o produto nao chegou", "produto nao chegou",
        "nao chegou", "nao foi entregue", "nao recebido", "produto ainda nao chegou", "nao recebi ainda",
        "nao recebi a mercadoria", "nao recebi nao recebi", "ate agora nao recebi", "produto ainda nao entregue",
        "nao recebemos o produto", "nao recebi meus produtos", "estou aguardando o produto", "estou aguardando",
        "ainda estou aguardando", "aguardo o produto")},
    **{p: DELAYED for p in (
        "demora na entrega", "muita demora na entrega", "atraso na entrega", "muito demorado",
        "entrega atrasada", "demora muito")},
    "produto com defeito": _issue("quality_defect", "high", "defective product"),
    "produto veio com defeito": _issue("quality_defect", "high", "the product came defective"),
    "produto nao funciona": _issue("quality_defect", "high", "the product does not work"),
    "produto de pessima qualidade": _issue("quality_defect", "medium", "very poor quality product"),
    "produto de baixa qualidade": _issue("quality_defect", "medium", "low quality product"),
    "produto de ma qualidade": _issue("quality_defect", "medium", "bad quality product"),
    "recebi o produto errado": _issue("wrong_product", "high", "I received the wrong product"),
    "o produto veio errado": _issue("wrong_product", "high", "the product came wrong"),
    "produto incompleto": _issue("missing_item", "high", "incomplete product"),
    "frete muito caro": _issue("other", "low", "freight is too expensive"),
}
RULE_SENTIMENT = {"high": -0.8, "medium": -0.6, "low": -0.3}


def normalize(text: str) -> str:
    """去重音、转小写、去标点、合并空格。用于规则匹配和相同文本去重。"""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def apply_rule(norm_text: str, original: str) -> dict | None:
    """命中规则则返回抽取结果，否则返回 None。evidence_quote 用原文整句，保证一定出自原文。"""
    if norm_text in NO_ISSUE_POSITIVE:
        return {"issues": [], "sentiment_score": NO_ISSUE_POSITIVE[norm_text],
                "actionable_for_seller": False, "confidence": 0.9}
    if norm_text in NO_ISSUE_NEGATIVE:
        return {"issues": [], "sentiment_score": NO_ISSUE_NEGATIVE[norm_text],
                "actionable_for_seller": False, "confidence": 0.8}
    if norm_text in RULE_ISSUES:
        rule = RULE_ISSUES[norm_text]
        issue = {"category": rule["category"], "severity": rule["severity"],
                 "evidence_quote": original, "evidence_quote_en": rule["english"]}
        return {"issues": [issue], "sentiment_score": RULE_SENTIMENT[rule["severity"]],
                "actionable_for_seller": True, "confidence": 0.9}
    return None


# ---------------------------------------------------------------------------
# 读取与分流
# ---------------------------------------------------------------------------
def load_reviews(csv_path: Path) -> pd.DataFrame:
    """读取评分 <= 3 的评价，按 review_id 去重，拼接标题和正文。"""
    if not csv_path.is_file():
        raise FileNotFoundError(f"找不到 {csv_path}（请先运行 python run_all.py --download）")
    raw = pd.read_csv(csv_path, dtype={"review_id": "string", "review_comment_title": "string",
                                       "review_comment_message": "string"})
    low = raw[raw["review_score"] <= 3]
    # 同一 review_id 出现在多行（对应多个订单），正文完全相同，只保留一行。
    reviews = low.drop_duplicates("review_id", keep="first").copy()
    log.info("评分 <= 3：%s 行，按 review_id 去重后 %s 条", f"{len(low):,}", f"{len(reviews):,}")
    title = reviews["review_comment_title"].fillna("").str.strip()
    message = reviews["review_comment_message"].fillna("").str.strip()
    # 标题常常就是结论（如 "Péssimo"），和正文一起给模型；两者都有时用换行分开。
    reviews["text"] = (title + "\n" + message).str.strip()
    reviews["norm"] = reviews["text"].map(normalize)
    return reviews.reset_index(drop=True)


def route_reviews(reviews: pd.DataFrame) -> pd.DataFrame:
    """给每条评价分配处理方式：no_text（无文本）、rule（规则）、llm（模型）。"""
    reviews = reviews.copy()
    reviews["route"] = "llm"
    reviews.loc[reviews["norm"] == "", "route"] = "no_text"
    rule_hit = reviews["norm"].isin(set(NO_ISSUE_POSITIVE) | set(NO_ISSUE_NEGATIVE) | set(RULE_ISSUES))
    reviews.loc[rule_hit & (reviews["route"] == "llm"), "route"] = "rule"
    counts = reviews["route"].value_counts().to_dict()
    llm = reviews[reviews["route"] == "llm"]
    # 去重按原文（不是归一化文本）：evidence_quote 必须是每条原文的子串，大小写、标点不同的文本要各自处理。
    log.info("分流：%s；模型路线中唯一原文 %s 条（相同原文只调用一次；若按归一化文本合并可再省到 %s 条）",
             counts, f"{llm['text'].nunique():,}", f"{llm['norm'].nunique():,}")
    return reviews


def text_key(text: str) -> str:
    """缓存键：prompt 版本 + 模型 + 原文。相同原文的评价共用一次模型结果。"""
    return hashlib.sha256(f"{PROMPT_VERSION}|{MODEL}|{text}".encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Prompt 与 API
# ---------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str]:
    """按整行匹配分节标题读取 system 和 user 两节（头部说明文字里即使提到标题也不会被误切）。"""
    content = path.read_text(encoding="utf-8")
    pattern = (r"^# === System Prompt ===[ \t]*\n(.*?)^# === User Prompt Template ===[ \t]*\n(.*?)"
               r"^# === Few-shot Examples")
    match = re.search(pattern, content, re.S | re.M)
    if match is None:
        raise ValueError("prompt_template.txt 缺少 System Prompt / User Prompt Template / Few-shot Examples 分节标题")
    system, user = match.group(1).strip(), match.group(2).strip()
    if "{reviews_json}" not in user:
        raise ValueError("prompt_template.txt 的 User Prompt Template 部分缺少 {reviews_json} 占位符")
    return system, user


def is_peak(moment: datetime) -> bool:
    utc = moment.astimezone(timezone.utc)
    return utc.weekday() < 5 and (1 <= utc.hour < 4 or 6 <= utc.hour < 10)


def cache_hit_tokens(usage: dict) -> int:
    """缓存命中的输入 token。DeepSeek 文档的字段是 prompt_cache_hit_tokens；
    兼容 OpenAI 风格的 prompt_tokens_details.cached_tokens。"""
    if "prompt_cache_hit_tokens" in usage:
        return int(usage["prompt_cache_hit_tokens"] or 0)
    details = usage.get("prompt_tokens_details") or {}
    return int(details.get("cached_tokens") or 0)


def usage_cost(usage: dict, moment: datetime) -> float:
    """按 API 返回的 usage 计算费用；字段缺失时把全部输入按未命中缓存计（偏保守）。"""
    price = PRICES["peak" if is_peak(moment) else "off_peak"]
    hit = cache_hit_tokens(usage)
    miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
    out = usage.get("completion_tokens", 0)
    return (hit * price["cache_hit"] + miss * price["cache_miss"] + out * price["output"]) / 1_000_000


@dataclass
class Stats:
    """全部请求的累计用量，多线程共享。"""
    requests: int = 0
    failed_requests: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    peak_requests: int = 0
    dropped_ungrounded: int = 0
    dropped_invalid: int = 0
    failed_texts: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, usage: dict, moment: datetime) -> None:
        with self.lock:
            self.requests += 1
            self.peak_requests += int(is_peak(moment))
            hit = cache_hit_tokens(usage)
            self.cache_hit_tokens += hit
            self.cache_miss_tokens += usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0) - hit)
            self.output_tokens += usage.get("completion_tokens", 0)
            self.cost_usd += usage_cost(usage, moment)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "lock"}


class ApiClient:
    def __init__(self, api_key: str, system: str, user_template: str, stats: Stats):
        self.session = requests.Session()
        # 连接池大小与并发线程数一致；默认 10 个会在 24 线程下反复丢弃连接（首次全量运行时日志里出现过警告）。
        adapter = requests.adapters.HTTPAdapter(pool_connections=WORKERS, pool_maxsize=WORKERS)
        self.session.mount("https://", adapter)
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        self.system, self.user_template, self.stats = system, user_template, stats

    def call(self, items: list[dict]) -> str:
        """发送一批评价，返回模型输出的文本。网络错误、429、5xx 指数退避重试。"""
        reviews_json = json.dumps(items, ensure_ascii=False)
        body = {
            "model": MODEL,
            "messages": [{"role": "system", "content": self.system},
                         {"role": "user", "content": self.user_template.replace("{reviews_json}", reviews_json)}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},   # 默认开启思考模式，会额外产生推理 token；本任务不需要
            "temperature": 0,                   # 非思考模式下生效，让输出尽量稳定
            "max_tokens": 8000,
        }
        for attempt in range(5):
            moment = datetime.now(timezone.utc)
            try:
                response = self.session.post(API_URL, json=body, timeout=180)
            except requests.RequestException as exc:
                log.warning("请求异常（第 %d 次）：%s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 200:
                payload = response.json()
                self.stats.add(payload.get("usage", {}), moment)
                return payload["choices"][0]["message"].get("content") or ""
            if response.status_code in (401, 402):
                raise RuntimeError(f"API 拒绝请求（{response.status_code}）：请检查 key 或余额。{response.text[:200]}")
            log.warning("HTTP %s（第 %d 次）：%s", response.status_code, attempt + 1, response.text[:200])
            time.sleep(2 ** attempt)
        with self.stats.lock:
            self.stats.failed_requests += 1
        return ""


# ---------------------------------------------------------------------------
# 输出校验
# ---------------------------------------------------------------------------
def grounded(quote: str, text: str) -> bool:
    """evidence_quote 是否出自原文：先比较原样（忽略大小写和多余空白），再比较去重音后的形式。"""
    squash = lambda s: re.sub(r"\s+", " ", s).strip().lower()
    return bool(quote.strip()) and (squash(quote) in squash(text) or normalize(quote) in normalize(text))


def clamp(value: object, low: float, high: float, default: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def validate_result(raw: dict, text: str, stats: Stats) -> dict:
    """把模型的一条输出整理成规定 schema；类别非法或证据不在原文中的 issue 直接丢弃并计数。"""
    issues = []
    for item in raw.get("issues") or []:
        if not isinstance(item, dict):
            continue
        category, severity = item.get("category"), item.get("severity")
        quote = str(item.get("evidence_quote") or "")
        if category not in CATEGORIES or severity not in SEVERITIES:
            with stats.lock:
                stats.dropped_invalid += 1
            continue
        if not grounded(quote, text):
            with stats.lock:
                stats.dropped_ungrounded += 1
            continue
        issues.append({"category": category, "severity": severity, "evidence_quote": quote,
                       "evidence_quote_en": str(item.get("evidence_quote_en") or "")})
    confidence = clamp(raw.get("confidence"), 0.0, 1.0, 0.5)
    if len(issues) < len(raw.get("issues") or []):
        confidence = min(confidence, 0.5)  # 有 issue 被丢弃，说明这条输出不完全可信
    return {
        "issues": issues,
        "sentiment_score": round(clamp(raw.get("sentiment_score"), -1.0, 1.0, 0.0), 3),
        "actionable_for_seller": bool(raw.get("actionable_for_seller")) and bool(issues),
        "confidence": round(confidence, 3),
    }


def parse_batch(content: str, batch: list[dict], stats: Stats) -> dict[str, dict]:
    """解析一批输出，返回 {本地 id: 校验后的结果}；缺失或无法解析的 id 不在返回值里。"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {}
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return {}
    texts = {item["id"]: item["text"] for item in batch}
    parsed = {}
    for raw in results:
        if isinstance(raw, dict) and str(raw.get("id")) in texts and str(raw.get("id")) not in parsed:
            local_id = str(raw["id"])
            parsed[local_id] = validate_result(raw, texts[local_id], stats)
    return parsed


# ---------------------------------------------------------------------------
# 模型调用主循环
# ---------------------------------------------------------------------------
def append_ledger(mode: str, stats: Stats) -> None:
    """记录一次运行的实际用量；只要发出过请求就记录（包括试跑和实验）。"""
    if stats.requests == 0 and stats.failed_requests == 0:
        return
    record = {"time": datetime.now().isoformat(timespec="seconds"), "mode": mode,
              **{k: v for k, v in stats.as_dict().items() if k != "failed_texts"}}
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_cache(path: Path) -> dict[str, dict]:
    cache = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                cache[record["key"]] = record["result"]
    return cache


def run_llm(texts: list[str], client: ApiClient, cache: dict[str, dict], batch_size: int, workers: int,
            budget: float, started: float) -> dict[str, dict]:
    """并发处理未缓存的文本。每批完成后立即追加写入缓存文件，中断后重跑不会重复付费。"""
    todo = [t for t in texts if text_key(t) not in cache]
    log.info("需要调用模型的唯一文本 %s 条（缓存已有 %s 条）", f"{len(todo):,}", f"{len(texts) - len(todo):,}")
    write_lock = threading.Lock()
    batches = [todo[i:i + batch_size] for i in range(0, len(todo), batch_size)]

    def process(batch_texts: list[str], attempt: int = 1) -> None:
        if client.stats.cost_usd >= budget:
            client.stats.failed_texts.extend(batch_texts)
            return
        if time.perf_counter() - started > TIME_LIMIT_SECONDS:
            client.stats.failed_texts.extend(batch_texts)
            return
        items = [{"id": str(i + 1), "text": t} for i, t in enumerate(batch_texts)]
        parsed = parse_batch(client.call(items), items, client.stats)
        missing = []
        with write_lock, CACHE_PATH.open("a", encoding="utf-8") as handle:
            for item in items:
                result = parsed.get(item["id"])
                if result is None:
                    missing.append(item["text"])
                    continue
                key = text_key(item["text"])
                cache[key] = result
                # 只存哈希键和结果，不存评价原文：缓存随仓库提交，不能变相再分发数据集内容。
                handle.write(json.dumps({"key": key, "result": result}, ensure_ascii=False) + "\n")
        if missing and attempt < 3:
            # 输出为空、不是合法 json 或漏掉了部分 id：只重发缺失的那几条，批次越重试越小。
            log.warning("一批中有 %d 条没有拿到有效结果，第 %d 次重试", len(missing), attempt)
            for start in range(0, len(missing), max(1, len(missing) // 2)):
                process(missing[start:start + max(1, len(missing) // 2)], attempt + 1)
        elif missing:
            client.stats.failed_texts.extend(missing)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(process, batch) for batch in batches]
        for future in as_completed(futures):
            future.result()
            done += 1
            if done % 20 == 0 or done == len(batches):
                log.info("进度 %d/%d 批；已花费 $%.4f；输出 token %s", done, len(batches),
                         client.stats.cost_usd, f"{client.stats.output_tokens:,}")
    return cache


# ---------------------------------------------------------------------------
# 组装输出
# ---------------------------------------------------------------------------
def assemble(reviews: pd.DataFrame, cache: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """按题目 schema 输出有文本的每条评价；另加 route、review_score 两个字段方便评估。"""
    output, pending = [], []
    for row in reviews[reviews["route"] != "no_text"].itertuples(index=False):  # 约 1.4 万行，组装 JSON 列表
        if row.route == "rule":
            result = apply_rule(row.norm, row.text)
        else:
            result = cache.get(text_key(row.text))
            if result is None:
                pending.append(row.text)
                continue
        output.append({"review_id": row.review_id, **result, "route": row.route, "review_score": int(row.review_score)})
    return output, pending


def estimate_cost(texts: list[str], system: str, batch_size: int) -> dict:
    """不调用 API 的粗略预估：按约 3.5 个字符 1 个 token 估算葡萄牙语，每条输出约 110 token。"""
    batches = -(-len(texts) // batch_size)
    system_tokens = len(system) / 3.5
    text_tokens = sum(len(t) for t in texts) / 3.5 + 12 * len(texts)  # 12：每条的 json 包装
    output_tokens = 110 * len(texts)
    estimates = {}
    for period, price in PRICES.items():
        cost = (system_tokens * price["cache_miss"] + system_tokens * (batches - 1) * price["cache_hit"]
                + text_tokens * price["cache_miss"] + output_tokens * price["output"]) / 1_000_000
        estimates[period] = round(cost, 3)
    return {"batches": batches, "input_tokens_est": int(system_tokens * batches + text_tokens),
            "output_tokens_est": output_tokens, "cost_usd_est": estimates}


# ---------------------------------------------------------------------------
# 实验与评估
# ---------------------------------------------------------------------------
def run_experiment(texts: list[str], client_factory, size: int) -> None:
    """同一批文本分别用"单条请求"和"每批 20 条"各跑一次，比较 token、费用和输出一致性。"""
    random.Random(SEED).shuffle(texts)
    sample = texts[:size]
    report = {}
    outputs = {}
    for label, batch_size in (("single", 1), ("batch", BATCH_SIZE)):
        stats = Stats()
        client = client_factory(stats)
        results = {}
        for start in range(0, len(sample), batch_size):
            items = [{"id": str(i + 1), "text": t} for i, t in enumerate(sample[start:start + batch_size])]
            parsed = parse_batch(client.call(items), items, stats)
            results.update({item["text"]: parsed.get(item["id"]) for item in items})
        outputs[label] = results
        append_ledger(f"experiment_{label}", stats)
        report[label] = {**{k: v for k, v in stats.as_dict().items() if k != "failed_texts"},
                         "cost_per_review_usd": stats.cost_usd / len(sample)}
    same = sum(1 for t in sample
               if outputs["single"].get(t) and outputs["batch"].get(t)
               and {i["category"] for i in outputs["single"][t]["issues"]} == {i["category"] for i in outputs["batch"][t]["issues"]})
    report["sample_size"] = len(sample)
    report["same_category_set"] = same
    report["same_category_rate"] = round(same / len(sample), 3)
    EXPERIMENT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("实验结果已写入 %s：%s", EXPERIMENT_PATH.name, json.dumps(report, ensure_ascii=False))


def export_sample(output: list[dict], reviews: pd.DataFrame, size: int) -> None:
    """按 路线 × 评分 分层抽样，导出待人工标注的表格。已有标注时拒绝覆盖。"""
    if SAMPLE_PATH.is_file():
        existing = pd.read_csv(SAMPLE_PATH, dtype=str).fillna("")
        if ((existing["human_categories"] != "") | (existing["human_note"] != "")).any():
            log.warning("%s 已有标注，不覆盖；如确需重新抽样，请先手动改名备份", SAMPLE_PATH.name)
            return
    frame = pd.DataFrame(output).merge(reviews[["review_id", "text"]], on="review_id")
    frame["stratum"] = frame["route"] + "_" + frame["review_score"].astype(str)
    strata = sorted(frame["stratum"].unique())
    per = max(1, size // len(strata))
    sample = (frame.groupby("stratum", group_keys=False)
              .apply(lambda g: g.sample(min(per, len(g)), random_state=SEED)))  # 只对几个分层分组抽样
    sample = pd.concat([sample, frame.drop(sample.index).sample(max(0, size - len(sample)), random_state=SEED)])
    sample["model_categories"] = sample["issues"].map(lambda xs: ";".join(sorted({x["category"] for x in xs})))
    sample["model_quotes"] = sample["issues"].map(lambda xs: " | ".join(x["evidence_quote"] for x in xs))
    sample["human_categories"] = ""
    sample["human_note"] = ""
    columns = ["review_id", "route", "review_score", "text", "model_categories", "model_quotes",
               "sentiment_score", "actionable_for_seller", "human_categories", "human_note"]
    sample[columns].to_csv(SAMPLE_PATH, index=False, encoding="utf-8")
    log.info("已导出 %d 条标注样本到 %s；请在 human_categories 填写类别（多个用 ; 分隔，没有问题留空并在 human_note 写 none）",
             len(sample), SAMPLE_PATH.name)


def evaluate() -> None:
    """读取人工标注，计算整条完全一致率和各类别的精确率、召回率。"""
    sample = pd.read_csv(SAMPLE_PATH, dtype=str).fillna("")
    labeled = sample[(sample["human_categories"] != "") | (sample["human_note"].str.lower().str.strip() == "none")]
    if labeled.empty:
        raise SystemExit(f"{SAMPLE_PATH.name} 还没有标注")
    as_set = lambda s: {c.strip() for c in s.split(";") if c.strip()}
    pairs = [(as_set(r.model_categories), as_set(r.human_categories)) for r in labeled.itertuples()]
    exact = sum(m == h for m, h in pairs)
    per_category = {}
    for category in sorted(CATEGORIES):
        tp = sum(category in m and category in h for m, h in pairs)
        fp = sum(category in m and category not in h for m, h in pairs)
        fn = sum(category not in m and category in h for m, h in pairs)
        if tp + fp + fn:
            per_category[category] = {"tp": tp, "fp": fp, "fn": fn,
                                      "precision": round(tp / (tp + fp), 3) if tp + fp else None,
                                      "recall": round(tp / (tp + fn), 3) if tp + fn else None}
    tp_all = sum(v["tp"] for v in per_category.values())
    fp_all = sum(v["fp"] for v in per_category.values())
    fn_all = sum(v["fn"] for v in per_category.values())
    result = {
        "labeled": len(labeled), "exact_match": exact, "exact_match_rate": round(exact / len(labeled), 3),
        "micro_precision": round(tp_all / (tp_all + fp_all), 3) if tp_all + fp_all else None,
        "micro_recall": round(tp_all / (tp_all + fn_all), 3) if tp_all + fn_all else None,
        "per_category": per_category,
        "by_route": {route: round(sum(m == h for (m, h), r in zip(pairs, labeled["route"]) if r == route)
                                  / max(1, (labeled["route"] == route).sum()), 3)
                     for route in sorted(labeled["route"].unique())},
    }
    EVAL_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("评估结果已写入 %s：%s", EVAL_PATH.name, json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def get_api_key() -> str | None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=True)  # 以 .env 为准，避免终端里残留的旧环境变量
    except ImportError:
        pass
    return os.environ.get("DEEPSEEK_API_KEY") or None


def main() -> int:
    parser = argparse.ArgumentParser(description="Q4 评价结构化抽取")
    parser.add_argument("--dry-run", action="store_true", help="不调用 API，只输出分流统计和成本预估")
    parser.add_argument("--limit", type=int, help="只处理前 N 条需要模型的唯一文本（试跑用）")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=WORKERS)
    parser.add_argument("--budget", type=float, default=BUDGET_USD, help="本次运行的费用上限（美元）")
    parser.add_argument("--experiment", type=int, metavar="N", help="单条 vs 批量对比实验，抽 N 条")
    parser.add_argument("--export-sample", type=int, metavar="N", help="导出 N 条人工标注样本")
    parser.add_argument("--evaluate", action="store_true", help="根据人工标注计算准确率")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    if args.evaluate:
        evaluate()
        return 0

    started = time.perf_counter()
    reviews = route_reviews(load_reviews(REVIEWS_CSV))
    llm_texts = sorted(reviews.loc[reviews["route"] == "llm", "text"].unique())  # 排序保证每次顺序一致
    system, user_template = load_prompt(PROMPT_PATH)
    estimate = estimate_cost(llm_texts, system, args.batch_size)
    log.info("成本预估（全部未缓存时）：%s", estimate)
    if args.dry_run:
        return 0

    stats = Stats()
    api_key = get_api_key()
    make_client = lambda s: ApiClient(api_key, system, user_template, s)

    if args.experiment:
        if not api_key:
            raise SystemExit("实验需要 DEEPSEEK_API_KEY")
        run_experiment(list(llm_texts), make_client, args.experiment)
        return 0

    cache = load_cache(CACHE_PATH)
    targets = llm_texts[:args.limit] if args.limit else llm_texts
    if api_key:
        log.info("当前为%s时段", "高峰" if is_peak(datetime.now(timezone.utc)) else "非高峰（半价）")
        cache = run_llm(targets, make_client(stats), cache, args.batch_size, args.workers, args.budget, started)
    else:
        log.warning("没有找到 DEEPSEEK_API_KEY：只使用缓存结果，不调用 API")

    output, pending = assemble(reviews, cache)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    elapsed = time.perf_counter() - started
    routes = reviews["route"].value_counts().to_dict()
    run_stats = {
        "finished_at": datetime.now().isoformat(timespec="seconds"), "model": MODEL, "prompt_version": PROMPT_VERSION,
        "batch_size": args.batch_size, "workers": args.workers, "elapsed_seconds": round(elapsed, 1),
        "reviews_score_le_3_dedup": len(reviews), "routes": routes,
        "llm_unique_texts": len(llm_texts), "written_reviews": len(output), "pending_reviews": len(pending),
        "this_run": {k: v for k, v in stats.as_dict().items() if k != "failed_texts"},
        "this_run_failed_texts": len(stats.failed_texts), "estimate": estimate,
    }
    append_ledger("limit" if args.limit else "full", stats)
    # 只有真正调用过 API 的全量运行才更新 run_stats.json，避免全部命中缓存的重跑把统计覆盖成 0。
    if args.limit is None and stats.requests > 0:
        STATS_PATH.write_text(json.dumps(run_stats, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("写出 %s：%s 条；本次调用 %d 次，花费 $%.4f，用时 %.1f 秒",
             OUTPUT_PATH.name, f"{len(output):,}", stats.requests, stats.cost_usd, elapsed)
    if pending:
        log.warning("有 %s 条评价没有模型结果（无 key 或调用失败），未写入输出", f"{len(pending):,}")
    if args.export_sample:
        export_sample(output, reviews, args.export_sample)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
