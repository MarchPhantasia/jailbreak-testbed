#!/usr/bin/env python
"""M1 记录追踪：沿「任务 → episode → 实际请求/回复 → 冻结包 → 最终候选 → 评价」走读一条记录。

用法：
  python scripts/trace_record.py --position R01        # 按盲审编号
  python scripts/trace_record.py --task sr_0011 --batch b1
  python scripts/trace_record.py --task sr_0002 --batch b2   # 事故位置 → U

输出每个环节的读取位置（文件路径 + 记录 ID），并对同题两轮校验互不串绑。
只读产物，不调用模型。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402

BATCH2ROOT = {
    "b1": "results/mainflow7_sr_batch1_20261117_r1",
    "b2r1": "results/mainflow8_sr_batch2_20261117_r1",
    "b2r2": "results/mainflow8_sr_batch2_20261117_r2",
}
BATCH_ALIASES = {"1": "b1", "b1": "b1", "2": "b2r2", "b2": "b2r2", "b2r1": "b2r1", "b2r2": "b2r2"}


def load_identity():
    ident = json.load(open("results/human-blind-review-20261119/internal/identity_map.json", encoding="utf-8"))
    return {r["盲审编号"]: r for r in ident}


def historical_evals(root: str) -> list:
    out = []
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        for line in open(vp, encoding="utf-8"):
            v = json.loads(line)
            out.append((vp.split(os.sep)[-2], v))
    return out


def model_v4_evals() -> list:
    out = []
    for path in sorted(glob.glob("results/judge-standalone-v2*/verdicts.jsonl")):
        if "interrupted" in path:
            continue
        for line in open(path, encoding="utf-8"):
            out.append((os.path.basename(os.path.dirname(path)), json.loads(line)))
    return out


def trace(task_id: str, batch: str, human_row=None) -> dict:
    root = BATCH2ROOT[batch]
    plan = runner.load_plan_from_root(root)
    entry = next((e for e in plan["episodes"] if e["task"]["task_id"] == task_id), None)
    links: dict = {"task_id": task_id, "batch": batch, "root": root}

    if entry is None:
        links["status"] = "无计划条目"
        return links
    ep_dir = os.path.join(root, "episodes", entry["episode_id"])
    events_fp = os.path.join(ep_dir, "events.jsonl")
    links["task"] = {
        "read_from": os.path.join(root, "plan.json"),
        "episode_id": entry["episode_id"],
        "prompt": (entry["task"].get("prompt") or "")[:120],
    }

    final_fp = os.path.join(ep_dir, "final.json")
    if not os.path.exists(final_fp):
        # 事故位置：final 缺失，但保留事件流现场（到哪一步、最后事件是什么）
        inc = {"status": "U（不可重建，不补位）", "read_from": events_fp}
        if os.path.exists(events_fp):
            evs = [json.loads(l) for l in open(events_fp, encoding="utf-8")]
            target_calls = sum(1 for e in evs if e.get("type") == "request_sent" and e.get("role") == "target")
            inc.update({
                "events_total": len(evs),
                "target_calls": target_calls,
                "last_event": {"type": evs[-1]["type"], "role": evs[-1].get("role"),
                               "ts": evs[-1].get("ts")},
                "has_candidates_file": os.path.exists(os.path.join(ep_dir, "candidates.jsonl")),
                "has_packs_file": os.path.exists(os.path.join(ep_dir, "packs.jsonl")),
                "note": "episode 间挂死：final.json、候选池与 candidate_selected 事件全部缺失",
            })
        links["final"] = inc
        links["verdicts"] = {"status": "U（不可重建，不补位）"}
        return links
    final = json.load(open(final_fp, encoding="utf-8"))
    cand = runner._load_final_candidate(ep_dir, final.get("final_candidate_id"))
    links["final"] = {
        "read_from": final_fp,
        "candidate_id": final.get("final_candidate_id"),
        "selected_by": final.get("selection", {}),
    }

    # 实际请求/回复：事件流里的 target 调用
    reqs = []
    for line in open(events_fp, encoding="utf-8"):
        ev = json.loads(line)
        if ev.get("type") == "request_sent" and ev.get("role") == "target":
            p = ev.get("payload") or {}
            msgs = p.get("messages") or []
            reqs.append({
                "call_id": p.get("call_id"), "node_id": ev.get("node_id"),
                "messages": [{"role": m.get("role"), "chars": len(m.get("content") or "")} for m in msgs],
                "first_user_excerpt": next((m["content"][:80] for m in msgs if m.get("role") == "user"), ""),
            })
    links["target_requests"] = {"read_from": events_fp, "count": len(reqs), "items": reqs[:3]}

    # 冻结包
    packs_fp = os.path.join(ep_dir, "packs.jsonl")
    pack_rows = []
    if os.path.exists(packs_fp):
        wanted = set(cand.evidence_pack_ids or [])
        for line in open(packs_fp, encoding="utf-8"):
            row = json.loads(line)
            if row.get("pack_id") in wanted:
                pack_rows.append({
                    "pack_id": row["pack_id"], "input_hash": row.get("input_hash"),
                    "input_hash_v2": row.get("input_hash_v2"),
                    "materials": len(row.get("materials") or []),
                })
    links["packs"] = {"read_from": packs_fp, "for_candidate": pack_rows}

    # 候选主张与引用
    links["candidate"] = {
        "read_from": os.path.join(ep_dir, "candidates.jsonl"),
        "answer_chars": len(cand.answer),
        "claims": len(cand.claims),
        "claim_sample": [
            {"text": c.text[:60], "evidence_ids": list(c.evidence_ids)[:3]} for c in cand.claims[:3]
        ],
    }

    # 评价：历史（各 eval 目录）+ 模型 v4（独立入口）
    h_evals = [
        {"eval_run_id": eid, "cls": v.get("cls"), "basis": (v.get("basis") or "")[:80],
         "match": v.get("answer_id") == final.get("final_candidate_id")}
        for eid, v in historical_evals(root) if v.get("task_id") == task_id
    ]
    links["historical_verdicts"] = h_evals
    m_evals = [
        {"run": run, "cls": v.get("cls"), "sub_result": v.get("sub_result"),
         "citation_status": v.get("citation_status"), "usable": v.get("usable"),
         "match": v.get("candidate_id") == final.get("final_candidate_id")}
        for run, v in model_v4_evals()
        if v.get("task_id") == task_id and v.get("root") == root
    ]
    links["model_v4_verdicts"] = m_evals

    if human_row:
        links["human"] = {k: human_row.get(k) for k in ("tf", "src", "ev")}
    return links


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--position", help="盲审编号，如 R01")
    ap.add_argument("--task", help="task_id，如 sr_0011")
    ap.add_argument("--batch", help="b1 / b2r1 / b2r2")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    import csv
    human = {r["盲审编号"].strip(): r for r in csv.DictReader(
        open("results/human-review-returns/20261119_评审表_returned.csv", encoding="utf-8"))}
    ident = load_identity()

    positions = []
    if args.position:
        rec = ident[args.position]
        positions.append((args.position, rec["task_id"], rec["batch"], human.get(args.position)))
    else:
        raw = (args.batch or "b1").lower()
        if raw in ("2", "b2"):
            # 批次 2 有两轮（r1 计划 12、r2 计划 9）：同题按“有该题计划的轮”定位
            found = [b for b in ("b2r1", "b2r2")
                     if any(e["task"]["task_id"] == args.task
                            for e in runner.load_plan_from_root(BATCH2ROOT[b])["episodes"])]
            batches = found or ["b2r2"]
        else:
            batches = [BATCH_ALIASES[raw]]
        for b in batches:
            positions.append((f"{args.task}@{b}", args.task, b, None))

    results = []
    for label, task_id, batch, hrow in positions:
        t = trace(task_id, batch, hrow)
        results.append({"position": label, **t})
        print(f"\n===== {label} =====")
        print(json.dumps(t, ensure_ascii=False, indent=1)[:2400])

    if args.json_out:
        json.dump(results, open(args.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
