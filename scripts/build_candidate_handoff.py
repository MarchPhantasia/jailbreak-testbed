#!/usr/bin/env python
"""冻结候选交接产物生成器。

产出（`--out-dir`，默认 results/candidate-handoff-<date>/）：
  handoff.json —— 单文件自包含交接：23 份候选各自的任务、最终候选（答案/claims/gaps/
    装配配置）、冻结包（materials/components/双哈希）、包引用的证据原文（带校验和）、
    文件路径、三通道判定（历史原判/人工推导（规则 v2）/模型 v4）；
    另含 1 个事故 U 位置记录与规则元数据。
  index.md —— 人类可读索引（不含答案正文，只有身份/哈希/判定摘要/路径）。

用途：下一阶段「Assembler 同包对照（固定 Judge）」直接读取本产物即可开工，
不依赖 results/ 目录结构知识；哈希链保证与在线运行产物一致可核。
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402

BATCH2ROOT = {
    "b1": "results/mainflow7_sr_batch1_20261117_r1",
    "b2r1": "results/mainflow8_sr_batch2_20261117_r1",
    "b2r2": "results/mainflow8_sr_batch2_20261117_r2",
}


def load_original_verdicts():
    out = {}
    for root in BATCH2ROOT.values():
        for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
            for line in open(vp, encoding="utf-8"):
                v = json.loads(line)
                if v.get("cls"):
                    out[(v["task_id"], v.get("answer_id"))] = {
                        "eval_run_id": vp.split(os.sep)[-2], "cls": v["cls"], "basis": v.get("basis") or "",
                    }
    return out


def load_model_verdicts():
    out = {}
    for path in sorted(glob.glob("results/judge-standalone-v2*/verdicts.jsonl")):
        if "interrupted" in path or "pass4" in path or "pass5" in path:
            pass  # pass4/pass5 也读：可用判定优先覆盖
        for line in open(path, encoding="utf-8"):
            row = json.loads(line)
            key = (row["task_id"], row["candidate_id"])
            prev = out.get(key)
            if prev is None or (not prev.get("usable") and row.get("usable")):
                out[key] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default="results/candidate-handoff-20261119")
    ap.add_argument("--identity", default="results/human-blind-review-20261119/internal/identity_map.json")
    ap.add_argument("--human", default="results/human-review-returns/20261119_评审表_returned.csv")
    ap.add_argument("--labels", default="results/human-review-returns/partial_success_labels_v2.json")
    args = ap.parse_args()

    ident = json.load(open(args.identity, encoding="utf-8"))
    human = {r["盲审编号"].strip(): r for r in csv.DictReader(open(args.human, encoding="utf-8"))}
    labels_doc = json.load(open(args.labels, encoding="utf-8"))
    labels = labels_doc["labels"]
    originals = load_original_verdicts()
    models = load_model_verdicts()

    os.makedirs(args.out_dir, exist_ok=True)
    entries, ev_index = [], {}
    for rec in sorted(ident, key=lambda r: r["盲审编号"]):
        rid = rec["盲审编号"]
        root = BATCH2ROOT[rec["batch"]]
        ep = os.path.join(root, "episodes", rec["episode_id"])
        plan = runner.load_plan_from_root(root)
        task_def = next(e["task"] for e in plan["episodes"] if e["task"]["task_id"] == rec["task_id"])
        cand = runner._load_final_candidate(ep, rec["candidate_id"])
        all_evidence = runner._read_evidence(ep, None)
        packs = []
        wanted = set(cand.evidence_pack_ids or [])
        used_ev = set()
        for line in open(os.path.join(ep, "packs.jsonl"), encoding="utf-8"):
            row = json.loads(line)
            if row.get("pack_id") in wanted:
                packs.append(row)
                for m in row.get("materials") or []:
                    for eid in m.get("evidence_ids") or []:
                        used_ev.add(eid)
        evidence_rows = [e for e in all_evidence if e["evidence_id"] in used_ev]
        for e in evidence_rows:
            ev_index[e["evidence_id"]] = e

        key = (rec["task_id"], rec["candidate_id"])
        h = human[rid]
        lb = labels.get(rid) or {}
        m = models.get(key) or {}
        o = originals.get(key) or {}
        entries.append({
            "盲审编号": rid,
            "position": {"batch": {"b1": 1, "b2r1": 2, "b2r2": 2}[rec["batch"]], "task_id": rec["task_id"]},
            "task": task_def,
            "candidate": {
                "candidate_id": cand.candidate_id,
                "answer": cand.answer,
                "claims": [{"claim_id": c.claim_id, "text": c.text, "evidence_ids": list(c.evidence_ids)}
                           for c in cand.claims],
                "gaps": list(cand.gaps),
                "evidence_pack_ids": list(cand.evidence_pack_ids),
                "assembly_config": cand.assembly_config,
                "end_state": cand.end_state,
                "fallback_render": cand.fallback_render,
            },
            "packs": [{"pack_id": p["pack_id"], "materials": p.get("materials") or [],
                       "components": p.get("components") or [], "gaps": p.get("gaps") or [],
                       "input_hash": p.get("input_hash"), "input_hash_v2": p.get("input_hash_v2")}
                      for p in packs],
            "evidence": evidence_rows,
            "verdicts": {
                "historical": {"eval_run_id": o.get("eval_run_id"), "cls": o.get("cls"), "basis": o.get("basis")},
                "human": {"tf": h["tf"], "src": h["src"], "ev": h["ev"],
                          "full_success": h["tf"] == "complete" and h["src"] == "met" and h["ev"] == "sufficient",
                          "partial_success_rule_v2": bool(lb.get("partial")),
                          "sub_results": lb.get("sub_results") or [],
                          "rule_applied": lb.get("rule_applied", ""),
                          "reason": h["关键理由（一句）"]},
                "model_v4": {"cls": m.get("cls"), "full_success": m.get("full_success"),
                             "partial_success": m.get("partial_success"),
                             "sub_result": m.get("sub_result"),
                             "citation_status": m.get("citation_status"),
                             "eval_run_id": m.get("eval_run_id")},
            },
            "paths": {
                "episode_dir": ep,
                "final": os.path.join(ep, "final.json"),
                "candidates": os.path.join(ep, "candidates.jsonl"),
                "packs": os.path.join(ep, "packs.jsonl"),
                "evidence": os.path.join(ep, "evidence.jsonl"),
            },
        })

    handoff = {
        "handoff_id": f"candidate-handoff-{time.strftime('%Y%m%d')}",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "purpose": "Assembler 同包对照（固定 Judge）的输入；单文件自包含，不依赖目录结构知识",
        "rule_v2": labels_doc["meta"]["rule"],
        "source_locks": {
            "human_review_csv_sha256": "a5efcfc9a172e1a16767e97f2d022412b4868126276230e7f2fad52a9efc1ac5",
            "human_summary_sha256": "2be545553fac38fb7cd21d6103ed0a5abed6cee91e381377e1315d3533cb80b0",
        },
        "candidates": entries,
        "incident_u": {
            "position": {"batch": 2, "task_id": "sr_0002"},
            "status": "U",
            "reason": "episode 间挂死导致 final.json、候选池与 candidate_selected 事件全部缺失；未补位",
        },
        "counts": {
            "candidates": len(entries),
            "packs": sum(len(e["packs"]) for e in entries),
            "evidence_rows_dedup": len(ev_index),
        },
    }
    with open(os.path.join(args.out_dir, "handoff.json"), "w", encoding="utf-8") as f:
        json.dump(handoff, f, ensure_ascii=False, indent=1)

    # 索引（不含答案正文）
    idx = [f"# 冻结候选交接索引（{handoff['handoff_id']}）", "",
           f"生成：{handoff['created_at']} | 候选 {len(entries)} + 事故 U 1 | "
           f"包 {handoff['counts']['packs']} | 去重证据 {handoff['counts']['evidence_rows_dedup']} 行", "",
           "| 卡 | 任务 | 批次 | 候选 | 包（input_hash_v2 前 8） | 历史原判 | 人工推导（规则 v2） | 模型 v4 |",
           "|---|---|---|---|---|---|---|---|"]
    for e in entries:
        v = e["verdicts"]
        packs_s = ",".join((p.get("input_hash_v2") or p.get("input_hash") or "?")[:8] for p in e["packs"])
        human_s = ("完整" if v["human"]["full_success"]
                   else ("部分" if v["human"]["partial_success_rule_v2"] else "其余"))
        model_s = v["model_v4"]["cls"] or "U"
        idx.append(f"| {e['盲审编号']} | {e['position']['task_id']} | {e['position']['batch']} | "
                   f"{e['candidate']['candidate_id'][:12]}… | {packs_s} | {v['historical']['cls']} | "
                   f"{human_s} | {model_s} |")
    idx += ["", f"事故 U：sr_0002 @ 批次 2（挂死，无候选池）", "",
            "完整内容（任务/答案/claims/包/证据原文/三通道判定）见 handoff.json；",
            "来源锁定：人工评审表 a5efcfc9…、评审总结 2be54555…；规则 v2 元数据在内。"]
    with open(os.path.join(args.out_dir, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(idx) + "\n")

    print(json.dumps({"out_dir": args.out_dir, **handoff["counts"],
                      "bytes": os.path.getsize(os.path.join(args.out_dir, "handoff.json"))}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
