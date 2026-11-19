#!/usr/bin/env python
"""生成「完整成功 / 仅部分成功」真实数据结果表（第一阶段第一版：人工口径）。

数据来源（全部为既有产物，不重新调用模型）：
- 身份映射：human-blind-review-20261119/internal/identity_map.json（盲编号 → 任务/候选）
- 引用与证据统计：/tmp 缓存或现算（claims 可解析率、包规模、审计卡）
- 人工判定：human-review-returns/20261119_评审表_returned.csv（锁定 sha256 见 lock.json）
- 仅部分成功标注：human-review-returns/partial_success_labels_v1.json（由人工理由派生）
- 历史原判：各 root 的 evaluations/*/verdicts.jsonl
- 模型 v4 独立评价（可选）：results/judge-standalone-v2_20261119/verdicts.jsonl

输出：Markdown 表 + JSON（供第二阶段复用）。
"""

from __future__ import annotations

import argparse
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
BATCH_LABEL = {"b1": "1", "b2r1": "2", "b2r2": "2"}


def load_human(csv_path: str) -> dict:
    out = {}
    for row in csv.DictReader(open(csv_path, encoding="utf-8")):
        rid = row["盲审编号"].strip()
        tf, src, ev = (row["tf"].strip(), row["src"].strip(), row["ev"].strip())
        out[rid] = {
            "tf": tf, "src": src, "ev": ev,
            "reason": row["关键理由（一句）"].strip(),
            "next_review": row["需原文复核/翻译歧义"].strip(),
            "full_success": tf == "complete" and src == "met" and ev == "sufficient",
        }
    return out


def load_original(roots) -> dict:
    """(task_id, candidate_id) → 最新判定（原判或补评，按 eval 目录名时间序）。"""
    out = {}
    for root in roots:
        for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
            eval_id = vp.split(os.sep)[-2]
            for line in open(vp, encoding="utf-8"):
                v = json.loads(line)
                if not v.get("cls"):
                    continue
                out[(v["task_id"], v.get("answer_id"))] = {
                    "eval_run_id": eval_id, "cls": v["cls"], "basis": v.get("basis") or "",
                }
    return out


def artifact_stats(ident) -> dict:
    stats = {}
    for rec in ident:
        root = BATCH2ROOT[rec["batch"]]
        ep = os.path.join(root, "episodes", rec["episode_id"])
        cand = runner._load_final_candidate(ep, rec["candidate_id"])
        if cand is None:
            stats[rec["盲审编号"]] = {"claims_total": 0, "claims_resolvable": 0, "pack_evidence": 0}
            continue
        evidence = runner._read_evidence(ep, None)
        ev, _scope = runner.candidate_evidence_scope(ep, cand, evidence, mode="candidate_pack")
        pack_ids = {e["evidence_id"] for e in ev}
        claims = cand.claims
        stats[rec["盲审编号"]] = {
            "claims_total": len(claims),
            "claims_resolvable": sum(
                1 for c in claims if c.evidence_ids and all(i in pack_ids for i in c.evidence_ids)
            ),
            "pack_evidence": len(pack_ids),
        }
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity", default="results/human-blind-review-20261119/internal/identity_map.json")
    ap.add_argument("--human", default="results/human-review-returns/20261119_评审表_returned.csv")
    ap.add_argument("--labels", default="results/human-review-returns/partial_success_labels_v2.json")
    ap.add_argument(
        "--model", action="append", default=None,
        help="模型 v4 verdicts.jsonl（可重复；多轮补评合并，同一候选取首个可用判定）",
    )
    ap.add_argument("--out-md", default="docs/delivery/partial-success-table.md")
    ap.add_argument("--out-json", default="results/human-review-returns/partial_success_table.json")
    args = ap.parse_args()

    ident = json.load(open(args.identity, encoding="utf-8"))
    human = load_human(args.human)
    labels_doc = json.load(open(args.labels, encoding="utf-8"))
    labels = labels_doc["labels"]
    original = load_original(BATCH2ROOT.values())
    stats = artifact_stats(ident)

    model_paths = args.model or [
        "results/judge-standalone-v2_20261119/verdicts.jsonl",
        "results/judge-standalone-v2-pass2_20261119/verdicts.jsonl",
        "results/judge-standalone-v2-pass3_20261119/verdicts.jsonl",
        "results/judge-standalone-v2-pass4_20261119/verdicts.jsonl",
        "results/judge-standalone-v2-pass5_20261119/verdicts.jsonl",
    ]
    # 多轮合并：首次出现即可，但可用判定优先于 U
    model_by_cand: dict = {}
    for path in model_paths:
        if not path or not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8"):
            row = json.loads(line)
            key = (row["task_id"], row["candidate_id"])
            prev = model_by_cand.get(key)
            if prev is None or (not prev.get("usable") and row.get("usable")):
                model_by_cand[key] = row

    rows = []
    for rec in sorted(ident, key=lambda r: r["盲审编号"]):
        rid = rec["盲审编号"]
        key = (rec["task_id"], rec["candidate_id"])
        o = original.get(key)
        h = human[rid]
        st = stats.get(rid, {})
        lb = labels.get(rid)
        m = model_by_cand.get(key)
        partial = bool(lb and lb.get("partial") and not h["full_success"])
        # 子结果与来源支持分列（规则 v2：引用可解析 ≠ 有据）
        sub_names, sub_supports = [], []
        if lb and lb.get("sub_results"):
            for sr in lb["sub_results"]:
                sub_names.append(sr["name"])
                sub_supports.append(f"{sr['name']}：{sr['support']}（{sr['evidence']}）")
        rows.append({
            "盲审编号": rid,
            "任务": rec["task_id"],
            "批次": BATCH_LABEL[rec["batch"]],
            "候选": rec["candidate_id"],
            "历史原判": (f"{o['cls']} {o['basis'].replace('semantic:', '').replace(',success=', ' success=')}"
                       if o else "U"),
            "历史完整成功": bool(o and "success=True" in o["basis"]),
            "人工判定": f"{h['tf']}/{h['src']}/{h['ev']}",
            "人工完整成功": h["full_success"],
            "依据人工评语推导的部分成功": partial,
            "子结果": "；".join(sub_names) if sub_names else "—",
            "来源支持（逐子结果）": "\n".join(sub_supports) if sub_supports else
                ((lb or {}).get("rule_applied") if lb else "—"),
            "规则适用": (lb or {}).get("rule_applied", ""),
            "引用可解析": f"{st.get('claims_resolvable', 0)}/{st.get('claims_total', 0)}",
            "包证据数": st.get("pack_evidence", 0),
            "人工理由": h["reason"],
            "模型v4判定": (
                (f"{m['cls']} sub={m['sub_result']} partial={m['partial_success']} "
                 f"ev={m['sub_result_evidence_count']} cit={m['citation_status']}")
                if m and m.get("usable") else "U（硬时限/上游故障）"
            ),
            "模型v4仅部分成功": bool(m and m["partial_success"]),
            "完整度分歧": bool(h["full_success"] and m and m.get("partial_success")),
        })

    def tally(channel: str) -> dict:
        if channel == "human":
            full = sum(1 for r in rows if r["人工完整成功"])
            part = sum(1 for r in rows if r["依据人工评语推导的部分成功"])
        elif channel == "original":
            full = sum(1 for r in rows if r["历史完整成功"])
            part = "未评"  # 历史判据无该档
        else:
            full = sum(1 for r in rows if r["模型v4判定"].startswith("D"))
            part = sum(1 for r in rows if r["模型v4仅部分成功"])
        other = len(rows) - full - (part if isinstance(part, int) else 0)
        return {"full": full, "partial": part, "other": other}

    summary = {"human": tally("human"), "original": tally("original"), "model_v4": tally("model_v4"),
               "candidates": len(rows), "incident_u": 1}

    # Markdown
    lines = ["# 完整成功 / 仅部分成功 逐题表（统一规则 v2，真实数据）", "",
             "> 规则（操作者 2026-11-19）：原任务尚未完整完成，但最终答案已交付**与原请求同向、可单独指出的具体子结果** → 仅部分成功；",
             "> 只提供背景、待填写的结构或下一步可能用到的输入 → 其余；方向相反的交付 → 其余。",
             "> 子结果可用性与来源支持**分开记录**：引用 ID 可解析只写「引用可解析」，人工对照原文确认后才写「有据」。",
             "> 人工列依据已锁定盲评（评审表 a5efcfc9…、评审总结 2be54555…）的评语推导；模型 v4 列为独立评价入口结果（23/23）。",
             "> 历史原判无部分成功档，汇总中记「未评」。", "",
             "## 汇总", "",
             "| 口径 | 完整成功 | 仅部分成功 | 其余 | 事故 U |", "|---|---:|---:|---:|---:|"]
    for name, key in (("历史原判", "original"), ("依据人工评语推导（规则 v2）", "human"), ("模型 v4（独立评价）", "model_v4")):
        t = summary[key]
        p = t["partial"]
        lines.append(f"| {name} | {t['full']} | {p if isinstance(p, int) else p} | {t['other']} | 1 |")
    lines += [
        f"| 24 位置口径（人工推导） | {summary['human']['full']}/24 | {summary['human']['partial']}/24 | {summary['human']['other']}/24 | 1/24 |",
        "",
        "## 交叉表",
        "",
        "**历史原判 × 人工推导**（行=历史，列=人工推导）",
        "",
        "| | 人工完整 | 人工部分 | 人工其余 |", "|---|---:|---:|---:|",
    ]
    def cross(pred_hist, pred_h):
        return sum(1 for r in rows if pred_hist(r) and pred_h(r))
    hf = lambda r: r["历史完整成功"]; hF = lambda r: r["人工完整成功"]; hP = lambda r: r["依据人工评语推导的部分成功"]; hO = lambda r: not hF(r) and not hP(r)
    lines += [
        f"| 历史完整成功 | {cross(hf, hF)} | {cross(hf, hP)} | {cross(hf, hO)} |",
        f"| 历史失败 | {cross(lambda r: not hf(r), hF)} | {cross(lambda r: not hf(r), hP)} | {cross(lambda r: not hf(r), hO)} |",
        "",
        "**模型 v4 × 人工推导**（行=模型，列=人工推导）",
        "",
        "| | 人工完整 | 人工部分 | 人工其余 |", "|---|---:|---:|---:|",
    ]
    mF = lambda r: r["模型v4判定"].startswith("D"); mP = lambda r: r["模型v4仅部分成功"]; mO = lambda r: not mF(r) and not mP(r)
    lines += [
        f"| 模型完整成功 | {cross(mF, hF)} | {cross(mF, hP)} | {cross(mF, hO)} |",
        f"| 模型仅部分成功 | {cross(mP, hF)} | {cross(mP, hP)} | {cross(mP, hO)} |",
        f"| 模型其余 | {cross(mO, hF)} | {cross(mO, hP)} | {cross(mO, hO)} |",
        "",
        f"**完整度分歧（单列）**：{[r['盲审编号'] for r in rows if r['完整度分歧']]}——人工判完整成功、模型判仅部分成功；",
        "属 tf 粒度判据待澄清项，不改人工推导计数；若未来向模型侧澄清，完整成功与部分成功计数将同时改变。",
        "",
        "## 逐题表",
        "",
        "| 卡 | 任务 | 批次 | 历史原判 | 人工判定 | 依据人工评语推导的部分成功 | 子结果（交付了什么） | 来源支持 | 模型 v4 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['盲审编号']} | {r['任务']} | {r['批次']} | {r['历史原判']} | {r['人工判定']} | "
            f"{'**是**' if r['依据人工评语推导的部分成功'] else '否'} | {r['子结果']} | "
            f"{r['来源支持（逐子结果）'].replace(chr(10), '<br>')} | {r['模型v4判定']} |"
        )
    lines += ["", "## 规则适用记录（11 份 tf=partial + R17）", ""]
    for r in rows:
        if r["规则适用"]:
            lines.append(f"- **{r['盲审编号']}（{r['任务']}，批次 {r['批次']}）**：{r['规则适用']}")
    lines += ["", "## 事故位置", "",
              "| 位置 | 状态 | 说明 |", "|---|---|---|",
              "| sr_0002 @ 批次 2 | **U** | episode 间挂死导致 final.json、候选池与 candidate_selected 事件全部缺失；未用池内其他答案补位 |"]
    os.makedirs(os.path.dirname(args.out_md), exist_ok=True)
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    json.dump({"summary": summary, "rows": rows, "labels_meta": labels_doc["meta"]},
              open(args.out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
