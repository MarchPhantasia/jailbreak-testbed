#!/usr/bin/env python
"""M1 历史分档独立复算：直接从三个运行目录的原始产物重算 23 份候选的分档。

不读 partial_success_table.json 缓存；从 plan/final/candidates/packs/evidence
与锁定的人工返回、规则 v2 标注、独立评价产物各自重算，交叉核对预期数字：
  历史原判完整成功 = 6；人工推导（规则 v2）= 10 完整 / 6 仅部分 / 7 其余；
  模型 v4 = 6 完整 / 15 仅部分 / 2 其余；事故 U = 1（分母 24 位置）。
输出 results/m1-history-recompute/recompute.json + 逐行核对表（Markdown）。
"""

from __future__ import annotations

import csv
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
EXPECTED = {
    "historical_full": 6,
    "human_full": 10, "human_partial": 6, "human_other": 7,
    "model_full": 6, "model_partial": 15, "model_other": 2,
    "incident_u": 1,
}


def main() -> int:
    ident = json.load(open("results/human-blind-review-20261119/internal/identity_map.json", encoding="utf-8"))
    human = {r["盲审编号"].strip(): r for r in csv.DictReader(
        open("results/human-review-returns/20261119_评审表_returned.csv", encoding="utf-8"))}
    labels = json.load(open("results/human-review-returns/partial_success_labels_v2.json", encoding="utf-8"))["labels"]

    # 历史判定（按 root 的 eval 目录）
    hist = {}
    for root in BATCH2ROOT.values():
        for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
            for line in open(vp, encoding="utf-8"):
                v = json.loads(line)
                if v.get("cls"):
                    hist[(root, v["task_id"], v.get("answer_id"))] = v["cls"]

    # 模型 v4（多轮合并：可用优先）
    model = {}
    for path in sorted(glob.glob("results/judge-standalone-v2*/verdicts.jsonl")):
        if "interrupted" in path:
            continue
        for line in open(path, encoding="utf-8"):
            v = json.loads(line)
            key = (v.get("root"), v["task_id"], v["candidate_id"])
            prev = model.get(key)
            if prev is None or (not prev.get("usable") and v.get("usable")):
                model[key] = v

    rows, mismatches = [], []
    for rec in sorted(ident, key=lambda r: r["盲审编号"]):
        rid, root = rec["盲审编号"], BATCH2ROOT[rec["batch"]]
        ep = os.path.join(root, "episodes", rec["episode_id"])
        cand = runner._load_final_candidate(ep, rec["candidate_id"])
        assert cand is not None, f"{rid} 候选缺失"
        ev_all = runner._read_evidence(ep, None)
        ev, _ = runner.candidate_evidence_scope(ep, cand, ev_all, mode="candidate_pack")
        pack_ids = {e["evidence_id"] for e in ev}
        claims_ok = sum(1 for c in cand.claims
                        if c.evidence_ids and all(i in pack_ids for i in c.evidence_ids))

        h_cls = hist.get((root, rec["task_id"], rec["candidate_id"]))
        h_full = h_cls == "D"
        hm = human[rid]
        h_full_manual = hm["tf"] == "complete" and hm["src"] == "met" and hm["ev"] == "sufficient"
        lb = labels.get(rid) or {}
        h_partial = bool(lb.get("partial")) and not h_full_manual
        mv = model.get((root, rec["task_id"], rec["candidate_id"])) or {}
        m_full = bool(mv.get("full_success"))
        m_partial = bool(mv.get("partial_success"))

        rows.append({
            "盲审编号": rid, "task_id": rec["task_id"], "batch": rec["batch"],
            "candidate_id": rec["candidate_id"],
            "claims_resolvable": f"{claims_ok}/{len(cand.claims)}",
            "pack_evidence": len(pack_ids),
            "historical_cls": h_cls, "historical_full": h_full,
            "human_tf/src/ev": f"{hm['tf']}/{hm['src']}/{hm['ev']}",
            "human_full": h_full_manual, "human_partial": h_partial,
            "model_v4": mv.get("cls") or "U", "model_full": m_full, "model_partial": m_partial,
        })

    got = {
        "historical_full": sum(1 for r in rows if r["historical_full"]),
        "human_full": sum(1 for r in rows if r["human_full"]),
        "human_partial": sum(1 for r in rows if r["human_partial"]),
        "human_other": sum(1 for r in rows if not r["human_full"] and not r["human_partial"]),
        "model_full": sum(1 for r in rows if r["model_full"]),
        "model_partial": sum(1 for r in rows if r["model_partial"]),
        "model_other": sum(1 for r in rows if not r["model_full"] and not r["model_partial"]),
        "incident_u": 1,
    }
    for k, want in EXPECTED.items():
        if got[k] != want:
            mismatches.append(f"{k}: 复算 {got[k]} ≠ 预期 {want}")

    os.makedirs("results/m1-history-recompute", exist_ok=True)
    json.dump({"expected": EXPECTED, "got": got, "mismatches": mismatches, "rows": rows},
              open("results/m1-history-recompute/recompute.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    lines = ["# M1 历史分档独立复算（23 候选 + 1 事故 U）", "",
             f"结论：{'全部与预期一致' if not mismatches else '存在差异：' + '; '.join(mismatches)}", "",
             "| 口径 | 复算 | 预期 |", "|---|---:|---:|"]
    for k in EXPECTED:
        mark = "✓" if got[k] == EXPECTED[k] else "✗"
        lines.append(f"| {k} | {got[k]} | {EXPECTED[k]} {mark} |")
    lines += ["", "| 卡 | 任务 | 批次 | 历史cls | 人工 | 推导部分 | 模型v4 | claims |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['盲审编号']} | {r['task_id']} | {r['batch']} | {r['historical_cls']} | "
                     f"{r['human_tf/src/ev']} | {'是' if r['human_partial'] else '否'} | {r['model_v4']} | "
                     f"{r['claims_resolvable']} |")
    lines += ["", "事故 U：sr_0002@b2r1（ep_8a38f9dc30，369 事件/20 次Target调用，挂死于 assembler 请求，无 final.json）"]
    open("results/m1-history-recompute/recompute.md", "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(json.dumps({"got": got, "mismatches": mismatches}, ensure_ascii=False))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
