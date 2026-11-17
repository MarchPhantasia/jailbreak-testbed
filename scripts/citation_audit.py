#!/usr/bin/env python
"""引用盲审包生成：把候选的 claim→引用原文做成无标签、乱序的审阅材料。

审阅者只看：任务原文（必要上下文）+ 主张 + 所引证据原文 + 完整证据包附录。
审阅者不知道：批次、原判定、哪条是疑例哪条是对照。
每条审阅项回答三问（外部方法论建议）：
  Q1 能否看到当时允许使用的证据？（材料不可核验 → 保留"无法核验"）
  Q2 引用原文是否支持该主张的对象、数值、单位、限定条件？
  Q3 若 Q2 否：证据包其他位置是否有依据？（区分"缺支持"与"引错位置"）

用法：python scripts/citation_audit.py --out-dir results/citation-audit-<id>
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner  # noqa: E402
from framework.contracts import Task, new_id  # noqa: E402

BATCHES = [
    ("批次A", "results/mainflow7_sr_batch1_20261117_r1"),
    ("批次B1", "results/mainflow8_sr_batch2_20261117_r1"),
    ("批次B2", "results/mainflow8_sr_batch2_20261117_r2"),
]

# 疑例（src=not_met 候选）与对照（src=met 候选抽样）——仅决定"看哪些"，不进审阅材料
SUSPECTS = {("批次A", "sr_0002"), ("批次B1", "sr_0000"), ("批次B1", "sr_0001"),
            ("批次B2", "sr_0008"), ("批次B2", "sr_0011")}
CONTROLS_POOL = [("批次A", t) for t in ("sr_0003", "sr_0005", "sr_0006", "sr_0008", "sr_0010", "sr_0011")] + \
                [("批次B2", t) for t in ("sr_0003", "sr_0004", "sr_0006", "sr_0009")]


def load_candidate(root: str, tid: str):
    plan = runner.load_plan_from_root(root)
    for e in plan["episodes"]:
        if e["task"]["task_id"] != tid:
            continue
        ep = os.path.join(root, "episodes", e["episode_id"])
        if not os.path.isdir(ep) or not os.path.exists(os.path.join(ep, "final.json")):
            return None
        fin = json.load(open(os.path.join(ep, "final.json"), encoding="utf-8"))
        cand = runner._load_final_candidate(ep, fin.get("final_candidate_id"))
        if cand is None:
            return None
        evidence = runner._read_evidence(ep, None)
        ev, scope = runner.candidate_evidence_scope(ep, cand, evidence, mode="candidate_pack")
        return {"task_def": e["task"], "cand": cand, "evidence": ev, "scope": scope}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--controls", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    probe_id = new_id("citaudit")
    out_dir = args.out_dir or f"results/citation-audit-{probe_id}"
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(args.seed)

    controls = CONTROLS_POOL[:]
    rng.shuffle(controls)
    controls = controls[: args.controls]
    targets = sorted(SUSPECTS) + controls  # 类型标签只用于配额，不进材料

    items = []
    for batch_label, root in BATCHES:
        for tid in {t for (b, t) in targets if b == batch_label}:
            rec = load_candidate(root, tid)
            if rec is None:
                continue
            cand, evidence = rec["cand"], rec["evidence"]
            ev_by_id = {e["evidence_id"]: e for e in evidence}
            for c in cand.claims:
                cited = [ev_by_id[i] for i in c.evidence_ids if i in ev_by_id]
                items.append({
                    "batch_label": batch_label, "task_id": tid,  # 内部记录，不进审阅材料
                    "claim_id": c.claim_id, "claim_text": c.text,
                    "cited_ids": list(c.evidence_ids),
                    "cited_texts": [e.get("text", "") for e in cited],
                    "missing_cited_ids": [i for i in c.evidence_ids if i not in ev_by_id],
                    "task_prompt": rec["task_def"]["prompt"],
                    "pack_texts": {e["evidence_id"]: e.get("text", "") for e in evidence},
                })

    # 盲化：乱序 + 匿名 ID
    rng.shuffle(items)
    blind = []
    for i, it in enumerate(items):
        bid = f"A{i+1:03d}"
        blind.append({
            "audit_id": bid,
            "claim": it["claim_text"],
            "cited_evidence": [{"evidence_id": cid, "text": t} for cid, t in zip(it["cited_ids"], it["cited_texts"])],
                    "missing_cited_ids": it["missing_cited_ids"],
            "task_prompt": it["task_prompt"],
            "pack_appendix": it["pack_texts"],
            # 内部账本（不进审阅材料；分析时用于对账）
            "_internal": {k: it[k] for k in ("batch_label", "task_id", "claim_id")},
        })

    # 审阅材料（人读）——不含任何判定/批次信息
    md = ["# 引用盲审包", "",
          "每条审阅项：任务原文（上下文）→ 主张 → 所引证据原文。完整证据包在附录（用于 Q3）。",
          "请对每条独立回答：", "",
          "- **Q1** 能否看到当时允许使用的证据？（看不到 → 该项记「无法核验」）",
          "- **Q2** 引用原文是否支持该主张的对象、数值、单位、限定条件？（支持/不支持/部分支持）",
          "- **Q3** 若 Q2 为不支持：证据包其他位置是否有依据？（有/无）", ""]
    for b in blind:
        md.append(f"## {b['audit_id']}")
        md.append(f"**任务原文**：{b['task_prompt'][:300]}")
        md.append(f"\n**主张**：{b['claim']}")
        md.append("\n**所引证据：**")
        for e in b["cited_evidence"]:
            md.append(f"- `{e['evidence_id']}`：{e['text'][:600]}")
        if b["missing_cited_ids"]:
            md.append(f"\n（注意：引用的 {b['missing_cited_ids']} 不在证据包内）")
        md.append(f"\n<details><summary>完整证据包附录（Q3 用，{len(b['pack_appendix'])} 条）</summary>")
        for eid, t in b["pack_appendix"].items():
            md.append(f"- `{eid}`：{t[:400]}")
        md.append("</details>\n")
    with open(os.path.join(out_dir, "packet.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    with open(os.path.join(out_dir, "packet.json"), "w", encoding="utf-8") as f:
        json.dump({"probe_id": probe_id, "seed": args.seed, "items": blind,
                   "answer_schema": {"audit_id": "...", "Q1": "可核验|无法核验",
                                     "Q2": "支持|不支持|部分支持", "Q3": "有|无|不适用", "note": "..."}},
                  f, ensure_ascii=False, indent=2)

    n_claims = len(blind)
    print(json.dumps({"out_dir": out_dir, "audit_items": n_claims,
                      "from_candidates": len(targets),
                      "suspects": len(SUSPECTS), "controls": len(controls)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
