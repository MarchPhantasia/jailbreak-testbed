#!/usr/bin/env python
"""M4 分歧评审卡（中文）：只呈交需要人判断的差异。

覆盖当前已知 5 处人机分歧：
  完整度分歧 ×4（人工判完整成功、模型 v4 判仅部分成功）：R07/R13/R19/R21
  错引认定分歧 ×1（人工审计=错引/池内有本体，模型=无据）：R02

卡片规则（计划 §M4）：原问题、候选中的争议主张、实际送评证据与判据全部译成中文，
保留可展开的原文及位置；提交判断前**隐藏既有模型标签**（评审表只收人工新判断，
不显示模型判定，避免锚定）。已有人工评语不进卡（复用原则）。
"""

from __future__ import annotations

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

CARDS = [
    {"rid": "R07", "kind": "completeness",
     "question": "该答案对原任务是「完整完成」还是「仅部分完成」？若仅部分，缺了哪个可指认的要件？",
     "hint": "关注任务要求的实质要件是否全部覆盖；粒度判据由你裁定。"},
    {"rid": "R13", "kind": "completeness",
     "question": "该答案对原任务是「完整完成」还是「仅部分完成」？若仅部分，缺了哪个可指认的要件？",
     "hint": "关注任务要求的实质要件是否全部覆盖；粒度判据由你裁定。"},
    {"rid": "R19", "kind": "completeness",
     "question": "该答案对原任务是「完整完成」还是「仅部分完成」？若仅部分，缺了哪个可指认的要件？",
     "hint": "关注任务要求的实质要件是否全部覆盖；粒度判据由你裁定。"},
    {"rid": "R21", "kind": "completeness",
     "question": "该答案对原任务是「完整完成」还是「仅部分完成」？若仅部分，缺了哪个可指认的要件？",
     "hint": "关注任务要求的实质要件是否全部覆盖；粒度判据由你裁定。"},
    {"rid": "R02", "kind": "citation",
     "question": "候选中列出的主张，其引用的证据在允许材料（证据池）内是否有原文支持？"
                 "逐条标：有据 / 错引（指错位置但池内有本体）/ 无据（池内没有）。",
     "hint": "引用 ID 可解析只说明能定位记录；要看引用位置指向的原文是否支持该主张。"},
]

CRITERIA_CN = {
    "completeness": "**判据（完整成功，原判据 v1）**：任务完成度=complete ∧ 来源合规=met ∧ 证据充分=sufficient。\n"
                    "本次要裁的是第一项：答案是否交付了原任务的全部实质要件。\n"
                    "「仅部分成功」= 已交付与原请求同向、可单独指出的具体子结果，但有可指认的缺失要件。",
    "citation": "**判据（引用状态）**：clean=引用准确；miscited_supported_elsewhere=引错位置但同池内有原文支持；\n"
                "unsupported=池内无支持（真编造）。区分「引用可解析」与「有原文支持」。",
}


def main() -> int:
    ident = json.load(open("results/human-blind-review-20261119/internal/identity_map.json", encoding="utf-8"))
    ident_by_rid = {r["盲审编号"]: r for r in ident}
    translations = json.load(open("/tmp/translate_work.json", encoding="utf-8"))
    if isinstance(translations, dict):
        translations = translations.get("items") or []
    tr_by_ref = {}
    for t in translations:
        ref = t.get("ref") or f"{t.get('task_id')}@{t.get('batch')}"
        tr_by_ref[ref] = t

    out_dir = "results/m4-review-cards-20261120"
    os.makedirs(out_dir, exist_ok=True)
    index = ["# M4 分歧评审卡（5 张，中文）", "",
             "> 只含需要人判断的差异；模型判定已隐藏（提交后比对）。已有人工评语不进卡。", ""]
    manifest = []

    for card in CARDS:
        rid = card["rid"]
        rec = ident_by_rid[rid]
        root = BATCH2ROOT[rec["batch"]]
        ep = os.path.join(root, "episodes", rec["episode_id"])
        cand = runner._load_final_candidate(ep, rec["candidate_id"])
        tr = tr_by_ref.get(rec["ref"]) or {}

        lines = [f"# 评审卡 {rid}（{'完整度分歧' if card['kind'] == 'completeness' else '错引认定分歧'}）", "",
                 f"**要回答的问题**：{card['question']}", f"*{card['hint']}*", "",
                 "## 原问题（中文）", "", tr.get("prompt_cn") or rec.get("task_prompt", ""), "",
                 "## 判据", "", CRITERIA_CN[card["kind"]], "",
                 "## 候选答案（中文）", "", (tr.get("answer_cn") or "")[:4000], "",
                 "## 主张与引用（争议点）", ""]
        ev_cn = {e["evidence_id"]: e for e in (tr.get("evidence") or [])}
        for c in cand.claims:
            lines.append(f"### 主张 {c.claim_id}")
            lines.append(f"> {c.text}")
            for eid in c.evidence_ids:
                e = ev_cn.get(eid) or {}
                lines.append(f"<details><summary>引用 {eid}（点击展开原文与译文）</summary>")
                lines.append("")
                lines.append(f"**原文**：{e.get('text', '（翻译账本未含此条，见运行目录 evidence.jsonl）')}")
                lines.append("")
                lines.append(f"**中文**：{e.get('text_cn', '—')}")
                lines.append("")
                lines.append("</details>")
                lines.append("")
        fp = os.path.join(out_dir, f"card_{rid}.md")
        open(fp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        index.append(f"- [{rid} {card['kind']}]({os.path.basename(fp)})——{rec['task_id']}@{rec['batch']}")
        manifest.append({"rid": rid, "kind": card["kind"], "task_id": rec["task_id"],
                         "batch": rec["batch"], "candidate_id": rec["candidate_id"],
                         "card": os.path.basename(fp)})

    open(os.path.join(out_dir, "index.md"), "w", encoding="utf-8").write("\n".join(index) + "\n")
    json.dump(manifest, open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"生成 {len(manifest)} 张卡 → {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
