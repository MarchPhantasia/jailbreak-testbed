#!/usr/bin/env python
"""refusal30 批次漏斗分析（2026-11-25）：直问基线 × A 臂 × C 臂三路对照。

只读聚合，0 模型调用。输出：
- 逐题三路对照表（直问标签 / A 臂结果 / C 臂结果 / 调用数 / 拒答数 / 恢复动作）
- 漏斗统计（拒答率→恢复→组件→最终档位）
- C 臂策略行为统计（指令规则分布、S 系列触发、delivered）
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WT = ROOT  # 全部产物在 worktree


def load_receipts(p: Path):
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def episode_summary(ep_dir: str) -> dict:
    evs = [json.loads(l) for l in open(f"{ep_dir}/events.jsonl", encoding="utf-8")]
    out = {
        "ep": os.path.basename(ep_dir),
        "target": sum(1 for e in evs if e["type"] == "request_sent" and e.get("role") == "target"),
        "controller": sum(1 for e in evs if e["type"] == "request_sent" and e.get("role") == "controller"),
        "assembler": sum(1 for e in evs if e["type"] == "request_sent" and e.get("role") == "assembler"),
        "refusals": [e for e in evs if e["type"] == "target_refusal"],
        "recovery": [e for e in evs if e["type"] == "recovery_decision"],
        "directives": [],
        "terminated": None,
        "final_candidate": None,
        "answer_chars": None,
        "fallback": None,
    }
    for e in evs:
        if e["type"] == "request_generated":
            pl = e.get("payload") or {}
            if pl.get("policy_directive"):
                out["directives"].append(pl["policy_directive"])
        elif e["type"] == "terminate":
            out["terminated"] = (e.get("payload") or {}).get("reason")
            out["final_candidate"] = (e.get("payload") or {}).get("final_candidate_id")
    fin = f"{ep_dir}/final.json"
    if os.path.exists(fin):
        f = json.loads(open(fin, encoding="utf-8").read())
        out["terminated"] = out["terminated"] or f.get("termination_reason")
        out["final_candidate"] = out["final_candidate"] or f.get("final_candidate_id")
    cand_file = f"{ep_dir}/candidates.jsonl"
    if os.path.exists(cand_file) and out["final_candidate"]:
        for line in open(cand_file, encoding="utf-8"):
            c = json.loads(line)
            if c.get("candidate_id") == out["final_candidate"]:
                out["answer_chars"] = len(c.get("answer") or "")
                out["fallback"] = c.get("fallback_render")
                break
    return out


def arm_summary(exp_id: str, manifest_rows: list) -> dict:
    root = WT / "results" / exp_id
    per_task = {}
    for i, meta in enumerate(manifest_rows):
        # episode 顺序即切片顺序（runner 逐题顺序执行）
        per_task[i] = None
    eps = sorted(glob.glob(str(root / "episodes" / "*")))
    # 按 plan.json 里的 episode→task 映射
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    task_of = {}
    for ep in plan["episodes"]:
        task_of[ep["episode_id"]] = ep["task"]["task_id"]
    for ep_dir in eps:
        ep_id = os.path.basename(ep_dir)
        tid = task_of.get(ep_id)
        idx = int(tid.replace("sr_", "")) if tid and tid.startswith("sr_") else None
        if idx is None:
            continue
        per_task[idx] = episode_summary(ep_dir)
    return per_task


def main() -> int:
    manifest = json.loads((WT / "data/slices/strongreject_dataset.refusal30.dev.manifest.json")
                          .read_text(encoding="utf-8"))
    rows = manifest["tasks"]
    direct = load_receipts(WT / "results/refusal30-direct-20261125/receipts.json")
    a = arm_summary("refusal30_a_20261125", rows)
    c = arm_summary("refusal30_c_20261125", rows)

    print(f"{'行':>4} {'直问':<10} {'A靶':>3} {'A拒':>3} {'A终':<22} {'C靶':>3} {'C拒':>3} {'C终':<22} {'C指令':>4}")
    for i, t in enumerate(rows):
        d = direct[i].get("rule_label", "?") if i < len(direct) else "?"
        ae, ce = a.get(i), c.get(i)
        g = lambda e, k: (str(e.get(k)) if e else "-")
        na = lambda e: (len(e["refusals"]) if e else "-")
        print(f"{t['source_row']:>4} {d:<10} "
              f"{g(ae,'target'):>3} {na(ae):>3} "
              f"{((ae['terminated'] or '-')[:22] if ae else '-'):<22} "
              f"{g(ce,'target'):>3} {na(ce):>3} "
              f"{((ce['terminated'] or '-')[:22] if ce else '-'):<22} "
              f"{(len(ce['directives']) if ce else 0):>4}")

    def agg(per):
        done = {i: e for i, e in per.items() if e}
        return {
            "episodes": len(done),
            "完成": sum(1 for e in done.values() if e["terminated"]),
            "target累计": sum(e["target"] for e in done.values()),
            "拒答事件累计": sum(len(e["refusals"]) for e in done.values()),
            "恢复决定累计": sum(len(e["recovery"]) for e in done.values()),
            "指令累计": sum(len(e["directives"]) for e in done.values()),
        }
    print("\nA 臂:", json.dumps(agg(a), ensure_ascii=False))
    print("C 臂:", json.dumps(agg(c), ensure_ascii=False))
    rules = Counter(d["rule"] for e in c.values() if e for d in e["directives"])
    print("C 指令规则分布:", dict(rules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
