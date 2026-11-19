#!/usr/bin/env python
"""R22 引用定位修复对照：把既有 A 候选的元行引用扩宽为材料完整跨度。

- 同一冻结包（原 R22 包，不改材料）；答案正文与主张文本**逐字不变**；
- 只重定位 evidence_ids（citation-relocate@v1，确定性，0 模型调用）；
- 产出 A'（新候选 ID）写入独立目录，随后用同固定 Judge 协议送评做修复前后对照。
"""
from __future__ import annotations
import json, os, shutil, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from framework import runner
from framework.assembly import relocate_meta_citations, CITATION_RELOCATION_VERSION
from framework.contracts import new_id, stable_hash, AnswerCandidate

SRC_EP = "results/r22-ab-20261120/episodes/ep_49f441c969"
SRC_CAND = "cand_217ebd143ff7"
OUT_ROOT = "results/r22-citefix2-20261120"

def main() -> int:
    if os.path.exists(os.path.join(OUT_ROOT, "plan.json")):
        raise SystemExit(f"{OUT_ROOT} 已存在")
    ev = {e["evidence_id"]: (e.get("text") or "") for e in runner._read_evidence(SRC_EP, None)}
    pack_row = json.loads(open(os.path.join(SRC_EP, "packs.jsonl"), encoding="utf-8").readline())
    A = runner._load_final_candidate(SRC_EP, SRC_CAND)
    assert A is not None
    answer_before, claims_text_before = A.answer, [c.text for c in A.claims]

    refs_before = sum(len(c.evidence_ids) for c in A.claims)
    claims, report = relocate_meta_citations(A.claims, pack_row["materials"], ev)
    # A' 构造：新 ID、正文与主张逐字不变
    from framework.assembly import Claim
    import dataclasses
    A2 = AnswerCandidate(
        candidate_id=new_id("cand"),
        answer=A.answer,
        claims=[Claim(claim_id=c.claim_id, text=c.text, evidence_ids=list(c.evidence_ids))
                for c in claims],
        evidence_pack_ids=list(A.evidence_pack_ids),
        gaps=list(A.gaps),
        assembly_config={**(A.assembly_config or {}),
                         "repaired_from": SRC_CAND,
                         "citation_relocation": {"version": CITATION_RELOCATION_VERSION,
                                                 "relocated": report,
                                                 "note": "答案正文与主张文本逐字不变；仅元行引用扩宽为材料完整跨度"}},
        stage_policy_ref=A.stage_policy_ref,
        processing_refs=list(A.processing_refs) + [f"repair:{CITATION_RELOCATION_VERSION}",
                                                   f"repaired_from:{SRC_CAND}"],
        fallback_render=False, end_state="assembled",
    )
    assert A2.answer == answer_before and [c.text for c in A2.claims] == claims_text_before, "正文/主张不得变化"

    ep = os.path.join(OUT_ROOT, "episodes", "ep_49f441c969")
    os.makedirs(ep, exist_ok=True)
    shutil.copyfile(os.path.join(SRC_EP, "evidence.jsonl"), os.path.join(ep, "evidence.jsonl"))
    with open(os.path.join(ep, "packs.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(pack_row, ensure_ascii=False, default=str) + "\n")
    row = {"candidate_id": A2.candidate_id, "answer": A2.answer,
           "claims": [{"claim_id": c.claim_id, "text": c.text, "evidence_ids": list(c.evidence_ids)}
                      for c in A2.claims],
           "evidence_pack_ids": A2.evidence_pack_ids, "gaps": A2.gaps,
           "assembly_config": A2.assembly_config, "fallback_render": False}
    with open(os.path.join(ep, "candidates.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    back = runner._load_final_candidate(ep, A2.candidate_id)
    assert back is not None and back.answer == A2.answer and len(back.claims) == len(A2.claims)
    plan = json.load(open(os.path.join(os.path.dirname(SRC_EP), "..", "plan.json"), encoding="utf-8"))
    entry = next(e for e in plan["episodes"] if e["episode_id"] == "ep_49f441c969")
    plan["experiment_id"] = "r22_citefix_20261120"
    plan["episodes"] = [entry]
    plan["note"] = "引用定位修复对照：A'（repaired A）+ 同冻结包；正文不变仅引用重定位"
    json.dump(plan, open(os.path.join(OUT_ROOT, "plan.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({"final_candidate_id": A2.candidate_id, "episode_id": "ep_49f441c969",
               "task_id": entry["task"]["task_id"],
               "final_candidate_pack_ids": list(A2.evidence_pack_ids),
               "termination_reason": "citefix_repair",
               "note": "A' = A 的引用定位修复（citation-relocate@v1），正文逐字不变"},
              open(os.path.join(ep, "final.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    total_refs_after = sum(len(c.evidence_ids) for c in A2.claims)
    print(json.dumps({"A'": A2.candidate_id, "relocated": len(report),
                      "refs_before": refs_before, "refs_after": total_refs_after,
                      "answer_identical": True}, ensure_ascii=False))
    json.dump({"A": SRC_CAND, "A_repaired": A2.candidate_id, "report": report,
               "refs_before": refs_before, "refs_after": total_refs_after},
              open(os.path.join(OUT_ROOT, "citefix_report.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for r in report:
        print(f"  {r['claim_id']}: {r['from']}（{r['kind']}）→ 材料跨度 {len(r['to'])} 行")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
