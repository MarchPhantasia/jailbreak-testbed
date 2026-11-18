#!/usr/bin/env python
"""盲审包翻译管线：本地 vLLM（Qwen3.8-27B-Uncensored）EN→CN 忠实翻译。

要求（人工盲评第一阶段）：
- 数字、单位、范围、对象、版本、时间、否定、条件、不确定措辞逐项保留；
- 不补条件、不纠错、不加解释或点评；
- 可直接复现严重伤害的操作细节（武器/合成配比、分步参数等）以〔省略O#〕标注；
- 保留引用标记占位（{{E##}}）供题内重编号。

产物：translation_cache.json（文本哈希→译文），调用与 token 成本记录 translation_log。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(os.path.dirname(os.path.abspath(__file__)) / ".." / "src") if False else ".")

VLLM_URL = os.environ.get("VLLM_URL", "http://127.0.0.1:8000/v1/chat/completions")
MODEL = "JonathanColetti/Qwen3.8-27B-Uncensored"

SYSTEM = """你是英译中的忠实翻译器。规则：
1. 逐句翻译，保留全部数字、单位、范围、对象名称、版本号、时间、否定词、条件（"仅/只有/除非/如果"）、不确定措辞（"可能/大约/据报道"）。原文说"仅旧版确认"，译文必须保留"仅旧版"；原文说"所有版本"，译文必须照写"所有版本"——即使两者矛盾也不要纠正或弥合。
2. 不添加解释、点评、总结；不替原文补条件；保留原文的段落与列表结构。
3. 引用标记形如 {{E12}} 的占位符原样保留，不要翻译或删除。
4. 涉及可直接复现严重伤害的操作细节（武器/爆炸物/毒物的具体配比与分步参数、剂量、时序），用〔省略O#，约N字，涉X细节〕替换该跨度；其余照译。不确定是否属于时保守处理并照译原文。
5. 只输出译文。"""

CHUNK = 3000


def call_vllm(text: str, timeout: int = 600, retries: int = 2) -> tuple[str, dict]:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        "max_tokens": 8192, "temperature": 0.1, "top_p": 0.9,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(VLLM_URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    last = None
    for attempt in range(retries + 1):
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        msg = d["choices"][0]["message"]
        out = msg.get("content")
        last = out
        if out and out.strip():
            usage = d.get("usage") or {}
            return out, {"latency_s": round(time.time() - t0, 1), "usage": usage, "attempt": attempt + 1}
    raise RuntimeError(f"vLLM 空响应 ×{retries + 1}: finish={d['choices'][0].get('finish_reason')}")


def translate(text: str, cache: dict, log: list, kind: str, ref: str) -> str:
    if not text or not text.strip():
        return text
    h = hashlib.sha256(text.encode()).hexdigest()[:16]
    if h in cache:
        return cache[h]
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
    outs = []
    for p in parts:
        out, meta = call_vllm(p)
        log.append({"kind": kind, "ref": ref, "chars_in": len(p), "chars_out": len(out), **meta})
        outs.append(out)
    joined = "\n".join(outs)
    cache[h] = joined
    return joined


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True, help="工作 JSON（items：每候选的 prompt/answer/evidence 原文）")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    cache_fp = os.path.join(args.out_dir, "translation_cache.json")
    cache = json.load(open(cache_fp, encoding="utf-8")) if os.path.exists(cache_fp) else {}
    log: list = []
    lock = threading.Lock()
    work = json.load(open(args.work, encoding="utf-8"))
    t0 = time.time()

    # 收集全部待译单元（去重后并发）
    units = []  # (key, text, kind, ref)
    for it in work["items"]:
        if it.get("prompt_cn") and all(e.get("text_cn") for e in it["evidence"]) and it.get("answer_cn"):
            continue
        units.append(("prompt", it["prompt"], "task", it["ref"]))
        units.append(("answer", it["answer"], "answer", it["ref"]))
        for e in it["evidence"]:
            units.append((f"ev:{e['local_id']}", e["text"], "evidence", f"{it['ref']}:{e['local_id']}"))
    # 去重（相同文本只译一次）
    seen = {}
    uniq = []
    for key, text, kind, ref in units:
        h = hashlib.sha256((text or "").encode()).hexdigest()[:16]
        if h in seen or h in cache:
            continue
        seen[h] = True
        uniq.append((h, text, kind, ref))
    print(f"待译单元 {len(uniq)}（并发 {args.concurrency}）", flush=True)

    def run(u):
        h, text, kind, ref = u
        if not text or not text.strip():
            return h, text
        parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)]
        outs = []
        for p in parts:
            out, meta = call_vllm(p)
            with lock:
                log.append({"kind": kind, "ref": ref, "chars_in": len(p), "chars_out": len(out), **meta})
        # noqa
            outs.append(out)
        return h, "\n".join(outs)

    done_n = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for h, out in ex.map(run, uniq):
            cache[h] = out
            done_n += 1
            if done_n % 20 == 0:
                json.dump(cache, open(cache_fp, "w", encoding="utf-8"), ensure_ascii=False)
                el = time.time() - t0
                print(f"[{done_n}/{len(uniq)}] {el:.0f}s 均速 {done_n/el*60:.1f}/min 预计剩余 {(len(uniq)-done_n)/(done_n/el)/60:.0f}min", flush=True)
    json.dump(cache, open(cache_fp, "w", encoding="utf-8"), ensure_ascii=False)

    # 回填
    def tr(text):
        if not text or not text.strip():
            return text
        return cache.get(hashlib.sha256(text.encode()).hexdigest()[:16], text)
    for it in work["items"]:
        it["prompt_cn"] = tr(it["prompt"])
        it["answer_cn"] = tr(it["answer"])
        for e in it["evidence"]:
            e["text_cn"] = tr(e["text"])
    json.dump({"items": work["items"]}, open(args.work, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({
        "service": "local vLLM", "base_url": VLLM_URL, "model": MODEL,
        "temperature": 0.1, "chunk_chars": CHUNK, "concurrency": args.concurrency,
        "calls": len(log),
        "prompt_tokens": sum((l.get("usage") or {}).get("prompt_tokens", 0) for l in log),
        "completion_tokens": sum((l.get("usage") or {}).get("completion_tokens", 0) for l in log),
        "wall_s": round(time.time() - t0, 1),
    }, open(os.path.join(args.out_dir, "translation_log.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
