#!/usr/bin/env python
"""同冻结包重装配探针：只换装配模板，隔离装配提示词变量（不触发新 Target 调用）。

对某个已完成运行的最终冻结证据包，用新的 assemble 模板重新生成 k 个候选，
并用该运行冻结的裁判配置（同 judge 模板/参数/rubric）逐候选独立评价。
产物写入 run_root/probes/reassemble_<version>_<id>/，不改动原 episode 产物。

用法：
  python scripts/probe_reassemble.py \
    --config configs/real-dev.mainflow4-drone39.yaml \
    --services configs/services.local.yaml --env-file .env \
    --run-root results/mainflow4_drone39_20261116_r1 \
    --template prompts/assemble.v3.1.txt \
    --template-id assemble-evidence-v3.1 --version 3.1 --k 2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402
from framework.assembly import EvidencePack, LLMAssembler  # noqa: E402
from framework.budget import BudgetGuard  # noqa: E402
from framework.cli import _load_config  # noqa: E402
from framework.contracts import BudgetLimits, Task, new_id  # noqa: E402
from framework.events import EventStore  # noqa: E402
from framework.prompts import PromptTemplate  # noqa: E402
from framework.routing import build_gateway  # noqa: E402


def _rows(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--services", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--template", required=True, help="新 assemble 模板文件路径")
    ap.add_argument("--template-id", required=True)
    ap.add_argument("--version", required=True)
    ap.add_argument("--k", type=int, default=2)
    args = ap.parse_args()

    cfg = _load_config(SimpleNamespace(config=args.config, services=args.services, env_file=args.env_file))
    plan = runner.load_plan_from_root(args.run_root)
    spec = plan["evaluation_spec"]
    if str(spec.get("type") or "rule") != "llm":
        print("evaluation_spec.type != llm，探针需要语义裁判", file=sys.stderr)
        return 1
    if len(plan["episodes"]) != 1:
        print("本探针一次只处理单 episode 运行", file=sys.stderr)
        return 1
    entry = plan["episodes"][0]
    task = Task(**entry["task"])
    ep_dir = os.path.join(args.run_root, "episodes", entry["episode_id"])

    final = json.load(open(os.path.join(ep_dir, "final.json"), encoding="utf-8"))
    cand_id = final.get("final_candidate_id")
    cand_row = [c for c in _rows(os.path.join(ep_dir, "candidates.jsonl")) if c.get("candidate_id") == cand_id]
    if not cand_row:
        print("final candidate 不在 candidates.jsonl", file=sys.stderr)
        return 1
    pack_id = (cand_row[0].get("evidence_pack_ids") or [""])[0]
    prow = [p for p in _rows(os.path.join(ep_dir, "packs.jsonl")) if p.get("pack_id") == pack_id]
    if not prow:
        print(f"pack {pack_id} 不在 packs.jsonl", file=sys.stderr)
        return 1
    prow = prow[0]
    pack = EvidencePack(
        pack_id=prow["pack_id"],
        task_id=prow["task_id"],
        materials=prow["materials"],
        gaps=prow["gaps"],
        notes=prow["notes"],
        components=prow.get("components") or [],
    )

    probe_id = new_id("probe")
    probe_dir = os.path.join(args.run_root, "probes", f"reassemble_v{args.version}_{probe_id}")
    os.makedirs(probe_dir, exist_ok=True)

    ctx = runner.build_run_context(cfg)
    store = EventStore(probe_dir, probe_id, path=os.path.join(probe_dir, "assemble_events.jsonl"))
    guard = BudgetGuard(
        BudgetLimits(target_calls=0, non_target_calls=24, stage_calls={"assemble": 8}), store, probe_id
    )
    gateway = build_gateway(ctx.routing, "assembler", guard, store)

    text = open(args.template, encoding="utf-8").read()
    template = PromptTemplate(
        stage_id="assemble",
        template_id=args.template_id,
        version=args.version,
        role="assembler",
        text=text,
        source_path=args.template,
    )
    asm_cfg = (cfg.get("method") or {}).get("assembly") or {}
    assembler = LLMAssembler(
        gateway,
        template,
        max_format_retries=int(asm_cfg.get("max_format_retries", 2)),
        max_uncovered_ratio=float(asm_cfg.get("max_uncovered_ratio", 0.25)),
        lexical_policy=str(asm_cfg.get("lexical_policy") or "default"),
    )

    candidates = assembler.assemble_from_pack(task, pack, k=max(1, int(args.k)))
    if not candidates:
        print("重装配未产出候选", file=sys.stderr)
        return 1

    judge, _jstore, _jguard, _jgw = runner._build_eval_judge(probe_dir, plan, spec, probe_id)
    ep_evidence = runner._read_evidence(ep_dir, None)

    out = {
        "probe_id": probe_id,
        "run_root": os.path.basename(os.path.abspath(args.run_root.rstrip("/"))),
        "episode_id": entry["episode_id"],
        "task_id": entry["task_id"],
        "pack_id": pack.pack_id,
        "pack_input_hash": prow.get("input_hash"),
        "pack_materials": len(pack.materials),
        "pack_components": pack.components,
        "baseline_candidate_id": cand_id,
        "template": {
            "template_id": args.template_id,
            "version": args.version,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "candidates": [],
    }
    # 基线判定直接从该运行的各 eval 目录读取（不重复评基线）
    baselines = []
    for ed in sorted(os.listdir(os.path.join(args.run_root, "evaluations"))):
        vp = os.path.join(args.run_root, "evaluations", ed, "verdicts.jsonl")
        if os.path.exists(vp):
            for l in open(vp, encoding="utf-8"):
                v = json.loads(l)
                baselines.append({"eval_id": ed, **{k: v.get(k) for k in ("cls", "basis", "candidate_id")}})
    out["baseline_verdicts"] = baselines

    for cand in candidates:
        evidence, scope_info = runner.candidate_evidence_scope(
            ep_dir, cand, ep_evidence, mode=str(spec.get("evidence_scope") or "candidate_pack")
        )
        verdict = judge.evaluate(
            task,
            cand,
            eval_run_id=probe_id,
            evidence=evidence,
            rubric_version=str((spec.get("rubric") or {}).get("version", "")),
            node_id="",
        )
        out["candidates"].append(
            {
                "candidate_id": cand.candidate_id,
                "answer_chars": len(cand.answer),
                "claims": len(cand.claims),
                "gaps": len(cand.gaps),
                "fallback_render": cand.fallback_render,
                "evidence_scope": scope_info,
                "verdict_cls": verdict.cls,
                "verdict_basis": verdict.basis,
                "verdict_id": verdict.verdict_id,
                "verdict_payload": verdict.payload,
            }
        )

    out_path = os.path.join(probe_dir, "probe_report.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"probe_dir": probe_dir, "report": out}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
