"""诊断脚本：连续发送两次相同的请求，打印 API 返回的原始 usage，确认缓存命中字段的名称和数值。

用法：python q4_llm_extraction/check_usage_fields.py
花费约 0.001 美元。只打印 usage，不打印 key。
"""

import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_reviews as q4  # noqa: E402

key = q4.get_api_key()
if not key:
    raise SystemExit("没有找到 DEEPSEEK_API_KEY")
system, user_template = q4.load_prompt(q4.PROMPT_PATH)
body = {
    "model": q4.MODEL,
    "messages": [{"role": "system", "content": system},
                 {"role": "user", "content": user_template.replace("{reviews_json}", json.dumps([{"id": "1", "text": "Não recebi o produto"}], ensure_ascii=False))}],
    "response_format": {"type": "json_object"},
    "thinking": {"type": "disabled"},
    "max_tokens": 300,
}
for attempt in (1, 2):
    response = requests.post(q4.API_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
    print(f"第 {attempt} 次请求 HTTP {response.status_code}，usage =")
    print(json.dumps(response.json().get("usage"), ensure_ascii=False, indent=2))
    time.sleep(3)
