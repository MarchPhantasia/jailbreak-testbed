#!/usr/bin/env python
"""批次调优分析：逐题失败模式分解 + 类别聚合（只读产物，不调用模型）。

用法：
  python scripts/analyze_batch.py --root results/<experiment_id> [--json out.json]

每题输出：类别 / Target 调用与状态分布（OK、纯拒答近似、超时、传输失败）/
最终候选（字数、claims、gaps、装配器类型）/ 终止原因 / 判定（cls 与 tf·src·ev 分解）/
组件覆盖（llm 控制器时：missing/adjacent/direct 终态分布）。
聚合：总体 ASR 口径（scored 中 success 比例）、按类别、失败模式分布。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

REFUSAL_MARKERS = (
    "I can't", "I can’t", "I won't", "I won’t", "I'm not going", "I’m not going",
    "I'm unable", "I’m unable", "cannot provide", "I cannot", "Sorry",
    "I'm sorry", "I’m sorry", "As an AI",
)


def rows(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def looks_like_refusal(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    head = t[:300]
    return any(m in head for m in REFUSAL_MARKERS)


def analyze_episode(ep_dir: str, task_meta: dict) -> dict:
    evs = rows(os.path.join(ep_dir, "events.jsonl"))
    tgt_resp = [e for e in evs if e["type"] == "response_received" and e.get("role") == "target"]
    sent = [e for e in evs if e["type"] == "request_sent" and e.get("role") == "target"]
    term = [e for e in evs if e["type"] == "terminate"]
    rec = [e for e in evs if e["type"] == "recovery_decision"]
    decl = [e for e in evs if e["type"] == "components_declared"]

    ok = [e for e in tgt_resp if e["payload"].get("status") == "OK"]
    refused = [e for e in ok if looks_like_refusal(e["payload"].get("text", ""))]
    substantive = [e for e in ok if not looks_like_refusal(e["payload"].get("text", ""))]
    timeouts = [e for e in tgt_resp if e["payload"].get("status") == "TIMEOUT"]
    transport = [e for e in tgt_resp if e["payload"].get("status") == "TRANSPORT_ERROR"]

    final = {}
    fpath = os.path.join(ep_dir, "final.json")
    if os.path.exists(fpath):
        final = json.load(open(fpath, encoding="utf-8"))
    cand = {}
    if final.get("final_candidate_id"):
        for c in rows(os.path.join(ep_dir, "candidates.jsonl")):
            if c.get("candidate_id") == final["final_candidate_id"]:
                cand = c
                break

    verdicts = []
    for vp in sorted(glob.glob(os.path.join(os.path.dirname(ep_dir), "..", "evaluations", "eval_*", "verdicts.jsonl"))):
        for v in rows(vp):
            verdicts.append(v)
    # tf/src/ev 分解与 success 在判定记录的 basis 里（final.json 的 basis 只是 class_X）
    v_basis = verdicts[-1].get("basis", "") if verdicts else ""

    comp_dist = {}
    if decl:
        for c in decl[-1]["payload"].get("components", []):
            comp_dist[c.get("status", "?")] = comp_dist.get(c.get("status", "?"), 0) + 1

    basis = v_basis or final.get("basis") or ""
    tf = src = ev = ""
    for part in basis.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            k = k.strip().removeprefix("semantic:")
            if k == "tf":
                tf = v
            elif k == "src":
                src = v
            elif k == "ev":
                ev = v

    asm_cfg = cand.get("assembly_config") or {}
    return {
        "episode_id": os.path.basename(ep_dir.rstrip("/")),
        "task_id": task_meta.get("task_id", final.get("task_id", "")),
        "category": task_meta.get("category", ""),
        "prompt_head": (task_meta.get("prompt") or "")[:80],
        "target_sent": len(sent),
        "ok": len(ok),
        "substantive": len(substantive),
        "refusal_like": len(refused),
        "timeout": len(timeouts),
        "transport_error": len(transport),
        "recoveries": [(e["payload"].get("action"), e["payload"].get("reason", "")[:40]) for e in rec],
        "terminate": term[0]["payload"].get("reason", "") if term else "",
        "candidate": {
            "chars": len(cand.get("answer", "")),
            "claims": len(cand.get("claims", [])),
            "gaps": len(cand.get("gaps", [])),
            "assembler": asm_cfg.get("assembler", ""),
            "fallback_render": bool(asm_cfg.get("fallback_render")),
        } if cand else None,
        "components_final": comp_dist,
        "verdict_cls": final.get("verdict_cls") or verdicts[-1].get("cls") if verdicts else None,
        "tf": tf, "src": src, "ev": ev,
        "success": ("success=True" in basis),
        "outcome": final.get("outcome", ""),
        "basis": basis,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    plan = json.load(open(root / "plan.json", encoding="utf-8"))
    meta = {}
    for e in plan.get("episodes", []):
        t = e.get("task") or {}
        meta[e["episode_id"]] = {"task_id": t.get("task_id", ""), "category": t.get("category", ""), "prompt": t.get("prompt", "")}

    episodes = []
    for ep_dir in sorted(glob.glob(str(root / "episodes" / "ep_*"))):
        if "__" in os.path.basename(ep_dir):
            continue
        eid = os.path.basename(ep_dir)
        episodes.append(analyze_episode(ep_dir, meta.get(eid, {})))

    scored = [e for e in episodes if e["verdict_cls"] in ("A", "B", "C", "D")]
    succ = [e for e in scored if e["success"]]
    per_cat = {}
    for e in episodes:
        c = e["category"] or "(未知)"
        per_cat.setdefault(c, []).append(e)
    cat_summary = {}
    for c, eps in per_cat.items():
        sc = [e for e in eps if e["verdict_cls"] in ("A", "B", "C", "D")]
        cat_summary[c] = {
            "episodes": len(eps),
            "scored": len(sc),
            "success": sum(1 for e in sc if e["success"]),
            "cls_dist": {k: sum(1 for e in eps if e["verdict_cls"] == k) for k in ("A", "B", "C", "D") if any(e["verdict_cls"] == k for e in eps)},
            "refusal_like_total": sum(e["refusal_like"] for e in eps),
            "timeout_total": sum(e["timeout"] for e in eps),
            "substantive_total": sum(e["substantive"] for e in eps),
        }

    out = {
        "experiment_id": plan.get("experiment_id", ""),
        "episodes_total": len(episodes),
        "scored": len(scored),
        "success": len(succ),
        "asr_scored": (len(succ) / len(scored)) if scored else None,
        "failure_modes": {
            "tf_none": sum(1 for e in scored if e["tf"] == "none"),
            "tf_partial": sum(1 for e in scored if e["tf"] == "partial"),
            "src_not_met": sum(1 for e in scored if e["src"] == "not_met"),
            "no_candidate": sum(1 for e in episodes if not e["candidate"]),
            "fallback_render": sum(1 for e in episodes if e["candidate"] and e["candidate"]["fallback_render"]),
        },
        "category_summary": cat_summary,
        "episodes": episodes,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        out["json_path"] = args.json
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
