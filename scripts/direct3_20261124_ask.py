#!/usr/bin/env python
"""StrongREJECT 三题直问（2026-11-24 验收方案 §4）。

同 round2 协议：独立会话、单条原始用户消息、temperature=0.0 /
reasoning_effort=low / max_tokens=65536。完整正文落盘 + 调用回执。
行为判读由测试人员读原文完成（本脚本只取数）。
"""
from __future__ import annotations
import hashlib, json, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "sr-direct3-20261124"
TASKS = [(8, "426c9b90da113091"), (192, "23412e8cba1562f9"), (193, "f779ad53d69486fe")]

def _chat(base, key, model, messages, **params):
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=json.dumps({"model": model, "messages": messages, **params}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read().decode())

def main() -> int:
    (OUT / "replies").mkdir(parents=True, exist_ok=True)
    env = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        env_path = Path("/home/pha/sci-deepseek/jailbreak-testbed/.env")
    for line in open(env_path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); env[k.strip()] = v.strip()
    rows = {int(json.loads(l)["id"]): json.loads(l)
            for l in open(ROOT / "data/slices/strongreject.direct3-20261124.jsonl", encoding="utf-8")}
    receipts = []
    for idx, prefix in TASKS:
        q = rows[idx]["forbidden_prompt"]
        h = hashlib.sha256(q.encode("utf-8")).hexdigest()
        assert h.startswith(prefix), f"行 {idx} 哈希不符"
        t0 = time.time()
        out = _chat("https://api.test.tokenca.ke/v1", env["TARGET_API_KEY"],
                    "deepseek/deepseek-v4.1-flash-1017",
                    [{"role": "user", "content": q}],
                    temperature=0.0, reasoning_effort="low", max_tokens=65536)
        wall = int((time.time() - t0) * 1000)
        reply = out["choices"][0]["message"].get("content") or ""
        (OUT / "replies" / f"row{idx}.txt").write_text(reply, encoding="utf-8")
        receipts.append({
            "source_row": idx, "question_sha256": h,
            "transport": "OK", "finish_reason": out["choices"][0].get("finish_reason"),
            "model": out.get("model", ""), "reply_chars": len(reply),
            "reply_text_sha256": hashlib.sha256(reply.encode()).hexdigest(),
            "reply_file": f"replies/row{idx}.txt",
            "tokens_in": out.get("usage", {}).get("prompt_tokens"),
            "tokens_out": out.get("usage", {}).get("completion_tokens"),
            "wall_ms": wall,
        })
        print(f"行{idx}: {len(reply)} 字符 wall={wall}ms finish={out['choices'][0].get('finish_reason')}")
    json.dump(receipts, open(OUT / "receipts.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("回执:", OUT / "receipts.json")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
