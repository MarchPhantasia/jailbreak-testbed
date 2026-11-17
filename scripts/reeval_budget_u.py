#!/usr/bin/env python
"""预算型 U 补评：对指定运行中因评价预算耗尽而未判定的任务，用冻结候选与冻结证据
以新评价 ID 重新调用语义裁判（零 Target 调用；不改写任何已有 eval 记录）。

用法：
  python scripts/reeval_budget_u.py --root results/<experiment_id> \
      --config configs/<同一实验的配置>.yaml --services configs/services.local.yaml \
      --env-file .env --tasks sr_0010 sr_0011
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner  # noqa: E402
from framework.cli import _load_config  # noqa: E402
from framework.contracts import Task, new_id  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--services", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--tasks", nargs="+", required=True)
    ap.add_argument("--budget", type=int, default=30, help="本次评价的裁判调用预算（默认 30）")
    args = ap.parse_args()

    cfg = _load_config(SimpleNamespace(config=args.config, services=args.services, env_file=args.env_file))
    root = str(Path(args.root).resolve())
    plan = runner.load_plan_from_root(root)
    spec = dict(plan["evaluation_spec"] or {})
    if str(spec.get("type")) != "llm":
        print("该运行不是 llm 评价", file=sys.stderr)
        return 1
    spec["budget"] = {"non_target_calls": args.budget}  # 仅内存副本：修正冻结时的预算配置错误

    eval_run_id = new_id("eval")
    eval_dir = os.path.join(root, "evaluations", eval_run_id)
    os.makedirs(eval_dir, exist_ok=True)
    judge, store, _g, _gw = runner._build_eval_judge(eval_dir, plan, spec, eval_run_id)

    ep_by_task = {e["task"]["task_id"]: e for e in plan["episodes"]}
    ep_evidence = runner._read_evidence  # 占位可读性
    out = []
    for tid in args.tasks:
        entry = ep_by_task.get(tid)
        if not entry:
            print(f"{tid}: 不在该运行的冻结计划中", file=sys.stderr)
            continue
        ep_dir = os.path.join(root, "episodes", entry["episode_id"])
        final_fp = os.path.join(ep_dir, "final.json")
        if not os.path.exists(final_fp):
            out.append({"task": tid, "skipped": "missing_final"})
            continue
        fin = json.load(open(final_fp, encoding="utf-8"))
        cand_id = fin.get("final_candidate_id")
        cand = runner._load_final_candidate(ep_dir, cand_id) if cand_id else None
        if cand is None:
            out.append({"task": tid, "skipped": "missing_candidate"})
            continue
        task = Task(**entry["task"])
        episode_evidence = runner._read_evidence(ep_dir, None)
        evidence, scope_info = runner.candidate_evidence_scope(
            ep_dir, cand, episode_evidence, mode=str(spec.get("evidence_scope") or "candidate_pack")
        )
        if not scope_info.get("integrity_complete", True):
            out.append({"task": tid, "skipped": "evidence_input_incomplete"})
            continue
        verdict = judge.evaluate(
            task, cand, eval_run_id=eval_run_id, evidence=evidence,
            rubric_version=str((spec.get("rubric") or {}).get("version", "v1")),
        )
        rec = {"task": tid, "cls": verdict.cls, "basis": verdict.basis, "verdict_id": verdict.verdict_id,
               "candidate_id": cand_id, "eval_run_id": eval_run_id}
        for part in (verdict.basis or "").split(","):
            k, _, v = part.strip().removeprefix("semantic:").partition("=")
            if k in ("tf", "src", "ev"):
                rec[k] = v
        out.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)

    with open(os.path.join(eval_dir, "ueval_summary.json"), "w", encoding="utf-8") as f:
        json.dump({"eval_run_id": eval_run_id, "reason": "budget_exhausted 补评（冻结输入不变，预算修正）",
                   "budget": args.budget, "results": out}, f, ensure_ascii=False, indent=2)
    print(json.dumps({"eval_dir": eval_dir, "judged": len([r for r in out if r.get("cls")])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
