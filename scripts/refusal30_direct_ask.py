#!/usr/bin/env python
"""refusal30 直问基线（2026-11-25）：30 题原题单发，行为判读存正文+回执。

协议与既往直问一致：独立会话、单条原始用户消息、temperature=0.0 /
reasoning_effort=low / max_tokens=65536。判读用修复后的 label_target 规则
留痕，正文全落盘供人工复核。
"""
from __future__ import annotations
import csv, hashlib, json, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from framework.envfile import load_env_file
from framework.judge import label_target

ROOT = Path(__file__).resolve().parent.parent
load_env_file(str(ROOT / ".env"))
OUT = ROOT / "results" / "refusal30-direct-20261125"
(OUT / "replies").mkdir(parents=True, exist_ok=True)

def _chat(base, key, model, messages, **params):
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "messages": messages, **params}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode())

env = {}
for line in open(ROOT / ".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()

rows = list(csv.DictReader(open(ROOT / "data/slices/strongreject_dataset.refusal30.dev.csv",
                                encoding="utf-8")))
receipts = []
for i, row in enumerate(rows):
    q = row["forbidden_prompt"]
    h = hashlib.sha256(q.encode("utf-8")).hexdigest()
    t0 = time.time()
    try:
        out = _chat("https://api.test.tokenca.ke/v1", env["TARGET_API_KEY"],
                    "deepseek/deepseek-v4.1-flash-1017",
                    [{"role": "user", "content": q}],
                    temperature=0.0, reasoning_effort="low", max_tokens=65536)
        reply = out["choices"][0]["message"].get("content") or ""
        finish = out["choices"][0].get("finish_reason")
        usage = out.get("usage", {})
    except Exception as e:  # 传输层事故单列
        (OUT / "replies" / f"idx{i:02d}_row?_error.txt").write_text(str(e), encoding="utf-8")
        receipts.append({"slice_index": i, "question_sha256": h, "transport": "ERROR",
                         "error": str(e)[:200]})
        print(f"idx{i:02d}: ERROR {str(e)[:60]}")
        continue
    lab = label_target(reply)
    (OUT / "replies" / f"idx{i:02d}.txt").write_text(reply, encoding="utf-8")
    receipts.append({
        "slice_index": i, "question_sha256": h, "transport": "OK",
        "finish_reason": finish, "rule_label": lab.value,
        "reply_chars": len(reply),
        "reply_text_sha256": hashlib.sha256(reply.encode()).hexdigest(),
        "reply_file": f"replies/idx{i:02d}.txt",
        "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
        "wall_ms": int((time.time() - t0) * 1000),
    })
    print(f"idx{i:02d}: {lab.value:<18} {len(reply):>5} 字符 finish={finish}")
json.dump(receipts, open(OUT / "receipts.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
from collections import Counter
print("标签分布:", dict(Counter(r.get("rule_label", "ERROR") for r in receipts)))
