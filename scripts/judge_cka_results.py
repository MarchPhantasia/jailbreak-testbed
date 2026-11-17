#!/usr/bin/env python
"""用本框架的冻结语义判据（tf/src/ev）评价外部（CKA）运行产物，并与其原判定并列输出。

对指定 CKA 运行目录的每个任务产物：
- 最终答案（final_output.txt）作为候选 answer；
- MCTS 树各节点观察（tree.json 的 observation）作为该候选的证据集（无 claim 引用，
  证据按未引用整条进入，符合 plan_evidence 规则）；
- 任务原文取自 planned_samples.json；
- 用与 mainflow 系列相同的 judge_eval.v2 模板与 rubric 调 SemanticJudge；
- 并列读取 CKA 原判定（final_judge_*.txt）供对比。

产物写入 out_dir（默认 results/cka-judge-probe_<id>/），不改动任何 CKA 原文件。
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
from framework.budget import BudgetGuard  # noqa: E402
from framework.cli import _load_config  # noqa: E402
from framework.contracts import (  # noqa: E402
    AnswerCandidate,
    BudgetLimits,
    Claim,
    Task,
    new_id,
)
from framework.evaluation import SemanticJudge  # noqa: E402
from framework.events import EventStore  # noqa: E402
from framework.prompts import PromptTemplate  # noqa: E402
from framework.routing import build_gateway  # noqa: E402
from types import SimpleNamespace


def load_cka_original_judge(a1_dir: str) -> dict:
    for fp in sorted(glob.glob(os.path.join(a1_dir, "final_judge_*.txt"))):
        try:
            return json.loads(open(fp, encoding="utf-8").read())
        except (ValueError, OSError):
            continue
    return {}


def load_cka_task(root: str, sample_id: str) -> dict:
    ps = json.load(open(os.path.join(root, "planned_samples.json"), encoding="utf-8"))
    rows = ps if isinstance(ps, list) else ps.get("samples", [])
    for r in rows:
        if r.get("sample_id", "").startswith(sample_id):
            return r
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cka-root", required=True, help="CKA 运行目录（含 artifacts/ 与 planned_samples.json）")
    ap.add_argument("--config", required=True, help="本框架任一 mainflow 配置（取 judge 模板/rubric/服务）")
    ap.add_argument("--services", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 个任务（0=全部）")
    args = ap.parse_args()

    cfg = _load_config(SimpleNamespace(config=args.config, services=args.services, env_file=args.env_file))
    runner.apply_service_overlay(cfg, "configs/services.local.yaml") if args.services else None
    ctx = runner.build_run_context(cfg)

    probe_id = new_id("ckajudge")
    out_dir = args.out_dir or f"results/cka-judge-probe_{probe_id}"
    os.makedirs(out_dir, exist_ok=True)
    store = EventStore(out_dir, probe_id, path=os.path.join(out_dir, "events.jsonl"))
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=200), store, probe_id)
    gateway = build_gateway(ctx.routing, "judge_eval", guard, store)

    prompt_fp = os.path.join(os.path.dirname(os.path.abspath(args.config)), cfg["prompts"]["judge_eval"]["path"])
    template = PromptTemplate(
        stage_id="judge_eval",
        template_id=cfg["prompts"]["judge_eval"]["id"],
        version=str(cfg["prompts"]["judge_eval"]["version"]),
        role="judge_eval",
        text=open(prompt_fp, encoding="utf-8").read(),
        source_path=prompt_fp,
    )
    judge = SemanticJudge(
        gateway,
        template,
        rubric=dict(cfg["evaluation"]["rubric"]),
        max_format_retries=int(cfg["evaluation"].get("max_format_retries", 1)),
        judge_ref=f"{template.template_id}@{template.version}",
        max_evidence_chars=int(cfg["evaluation"].get("max_evidence_chars", 40000)),
        evidence_scope="episode_pool",  # 外部候选无冻结包：证据整集进入，仅分析口径
        context_window=None,
        max_output_tokens=0,
    )

    art_dirs = sorted(glob.glob(os.path.join(args.cka_root, "artifacts", "*")))
    results = []
    for i, ad in enumerate(art_dirs):
        if args.limit and i >= args.limit:
            break
        sample_id = os.path.basename(ad)[:12]
        a1 = os.path.join(ad, "a1")
        if not os.path.isdir(a1):
            continue
        meta = load_cka_task(args.cka_root, sample_id)
        answer_fp = os.path.join(a1, "final_output.txt")
        if not os.path.exists(answer_fp) or not meta:
            results.append({"task": sample_id, "skipped": "missing_final_or_meta"})
            continue
        answer = open(answer_fp, encoding="utf-8").read()
        tree = json.load(open(os.path.join(a1, "tree.json"), encoding="utf-8"))
        evidence, ev_id = [], 0
        for nk, n in (tree.get("nodes") or {}).items():
            if nk == "root":
                continue
            obs = n.get("observation") or ""
            if obs.strip():
                ev_id += 1
                evidence.append({
                    "evidence_id": f"ckaN{ev_id:02d}",
                    "text": obs,
                    "kind": "external_observation",
                    "node": nk,
                })
        task = Task(task_id=f"cka_{sample_id}", prompt=meta.get("query", ""), primary_dataset="harmbench",
                    category=meta.get("category", ""), upstream_id=sample_id)
        cand = AnswerCandidate(
            candidate_id=new_id("ckacand"),
            answer=answer,
            claims=[Claim(claim_id="c_ext", text="(外部答案：整文作为单一主张对象，无 claim 级引用)", evidence_ids=[])],
            evidence_pack_ids=[],
            gaps=[],
            assembly_config={"assembler": "external_cka", "source": args.cka_root},
            end_state="assembled",
        )
        verdict = judge.evaluate(task, cand, eval_run_id=probe_id, evidence=evidence, rubric_version="v1")
        orig = load_cka_original_judge(a1)
        rec = {
            "task": sample_id,
            "query_head": meta.get("query", "")[:80],
            "ours": {
                "cls": verdict.cls,
                "basis": verdict.basis,
                "usable": verdict.usable,
                "verdict_id": verdict.verdict_id,
            },
            "cka_original": {k: orig.get(k) for k in ("classification", "confidence")},
            "answer_chars": len(answer),
            "evidence_count": len(evidence),
        }
        for part in (verdict.basis or "").split(","):
            k, _, v = part.strip().removeprefix("semantic:").partition("=")
            if k in ("tf", "src", "ev"):
                rec["ours"][k] = v
        results.append(rec)
        print(f"[{i+1}/{len(art_dirs)}] {sample_id} ours={verdict.cls} cka={orig.get('classification')} {verdict.basis[:60]}", flush=True)

    out = {"probe_id": probe_id, "cka_root": args.cka_root, "judge_ref": f"{template.template_id}@{template.version}",
           "evidence_mode": "episode_pool(外部观察整集)", "results": results}
    with open(os.path.join(out_dir, "comparison.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    ok = [r for r in results if r.get("ours", {}).get("cls")]
    agree = sum(1 for r in ok if r["ours"]["cls"] == r.get("cka_original", {}).get("classification"))
    print(json.dumps({"out_dir": out_dir, "judged": len(ok), "agree_with_cka": agree,
                      "ours_dist": {c: sum(1 for r in ok if r["ours"]["cls"] == c) for c in "ABCD"}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
