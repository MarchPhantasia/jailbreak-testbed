#!/usr/bin/env python
"""配对分析（从冻结计划逐条对账，显式绑定候选与 eval_run_id）。

规则（外部方法论建议固化）：
- 分母 = 冻结计划的全部任务位置（缺失保留为 U，不被目录枚举裁掉）；
- 批次 2 的 r1/r2 续跑按原任务身份合并（r1 跑了用 r1，否则用 r2）；
- 每个位置的判定显式绑定 (candidate_id, eval_run_id)：原判与补评分列，不互相覆盖；
- Target 调用数按 request_sent 事件计数；
- 输出 12 行配对表 + 调用总数 + 每成功成本（全批调用 ÷ 成功数）。

用法：python scripts/paired_analysis.py --out results/paired-analysis/<id>.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner  # noqa: E402

B1 = "results/mainflow7_sr_batch1_20261117_r1"
B2_ROOTS = ["results/mainflow8_sr_batch2_20261117_r1", "results/mainflow8_sr_batch2_20261117_r2"]


def positions_for(root: str):
    plan = runner.load_plan_from_root(root)
    out = {}
    for e in plan["episodes"]:
        ep = os.path.join(root, "episodes", e["episode_id"])
        rec = {"task_id": e["task"]["task_id"], "root": root, "episode_id": e["episode_id"]}
        if not os.path.isdir(ep):
            rec["status"] = "planned_not_run"
        elif not os.path.exists(os.path.join(ep, "final.json")):
            rec["status"] = "incident_no_final"
            rec["candidate_id"] = None
        else:
            fin = json.load(open(os.path.join(ep, "final.json"), encoding="utf-8"))
            rec["status"] = "rebuildable"
            rec["candidate_id"] = fin.get("final_candidate_id")
        if os.path.isdir(ep) and os.path.exists(os.path.join(ep, "events.jsonl")):
            rec["target_calls"] = sum(
                1 for l in open(os.path.join(ep, "events.jsonl"), encoding="utf-8")
                if (lambda e: e.get("type") == "request_sent" and e.get("role") == "target")(json.loads(l))
            )
        else:
            rec["target_calls"] = 0
        out[rec["task_id"]] = rec
    return out


def verdicts_for(root: str, task_id: str, candidate_id: str):
    """该位置的全部判定（按 eval 目录时间序），原判/补评/复评分列。"""
    rows = []
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        eval_id = vp.split(os.sep)[-2]
        for l in open(vp, encoding="utf-8"):
            v = json.loads(l)
            if v.get("task_id") == task_id and v.get("answer_id") == candidate_id and v.get("cls"):
                rows.append({"eval_run_id": eval_id, "cls": v["cls"], "basis": v.get("basis")})
    return rows


def merge_b2():
    """r1 跑了用 r1；否则 r2 兑现。r1 未兑现位置记录为被 r2 取代。"""
    p1 = positions_for(B2_ROOTS[0])
    p2 = positions_for(B2_ROOTS[1])
    merged, superseded = {}, {}
    for tid, rec in p1.items():
        if rec["status"] != "planned_not_run":
            merged[tid] = rec
        elif tid in p2:
            merged[tid] = p2[tid]
            superseded[tid] = "b2r1 计划未跑（挂死前中断），由 b2r2 兑现"
    for tid, rec in p2.items():
        merged.setdefault(tid, rec)
    return merged, superseded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    b1 = positions_for(B1)
    b2, superseded = merge_b2()

    table = []
    for tid in sorted(set(b1) | set(b2)):
        r1, r2 = b1.get(tid), b2.get(tid)
        row = {"task_id": tid, "batch1": None, "batch2": None}
        for rec, key in ((r1, "batch1"), (r2, "batch2")):
            if rec is None:
                continue
            cell = {"status": rec["status"], "root": rec["root"], "candidate_id": rec.get("candidate_id"),
                    "target_calls": rec.get("target_calls", 0)}
            if rec["status"] == "rebuildable":
                vs = verdicts_for(rec["root"], tid, rec["candidate_id"])
                cell["verdicts"] = vs  # 全部保留，不覆盖
                best = vs[-1] if vs else None
                cell["cls"] = best["cls"] if best else None
                cell["success"] = bool(best and "success=True" in (best.get("basis") or ""))
            else:
                cell["cls"] = None
                cell["success"] = False
                cell["verdicts"] = []
            row[key] = cell
        def s(c):
            if c is None or c["status"] == "planned_not_run":
                return "—"
            if c["status"] != "rebuildable":
                return "U"
            return "S" if c["success"] else "F"
        row["s1"], row["s2"] = s(row["batch1"]), s(row["batch2"])
        table.append(row)

    calls1 = sum(r["batch1"]["target_calls"] for r in table if r["batch1"])
    calls2 = sum(r["batch2"]["target_calls"] for r in table if r["batch2"])
    s1 = sum(1 for r in table if r["s1"] == "S")
    s2 = sum(1 for r in table if r["s2"] == "S")
    out = {
        "denominator": {"batch1": 12, "batch2": 12, "note": "缺失位置保留在分母（U），不被目录枚举裁掉"},
        "b2_merge": {"rule": "b2r1 跑了用 b2r1，否则 b2r2 兑现（同任务身份）", "superseded": superseded},
        "table": table,
        "summary": {
            "target_calls": {"batch1": calls1, "batch2": calls2},
            "successes": {"batch1": s1, "batch2": s2},
            "cost_per_success": {"batch1": round(calls1 / s1, 1) if s1 else None,
                                 "batch2": round(calls2 / s2, 1) if s2 else None},
            "sfu": {
                "batch1": {k: sum(1 for r in table if r["s1"] == k) for k in "SFU"},
                "batch2": {k: sum(1 for r in table if r["s2"] == k) for k in "SFU"},
            },
            "flips": {
                f"{r['s1']}→{r['s2']}": sum(1 for x in table if x["s1"] == r["s1"] and x["s2"] == r["s2"])
                for r in table
            },
        },
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"{'任务':8} | {'b1':6} {'调用':4} | {'b2':6} {'调用':4} | 翻转")
    for r in table:
        c1 = r["batch1"]["target_calls"] if r["batch1"] else 0
        c2 = r["batch2"]["target_calls"] if r["batch2"] else 0
        print(f"{r['task_id']:8} | {r['s1']:6} {c1:4} | {r['s2']:6} {c2:4} | {r['s1']}→{r['s2']}")
    print(f"\n调用：{calls1}/{calls2} | 成功：{s1}/{s2} | 每成功成本：{out['summary']['cost_per_success']}")
    print(f"翻转：{ {k:v for k,v in out['summary']['flips'].items() if v} }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
