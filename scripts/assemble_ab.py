#!/usr/bin/env python
"""同一冻结包上的 Assembler 配置 A/B（支持固定真实样例）。

固定：包身份与输入哈希、材料顺序、Assembler 模型与采样参数、每臂候选数（k=1）、
选择规则（单候选即选中）、最终 Judge 协议（rubric v2 + judge_eval.v4，走独立入口）。

两种包来源：
  --sample data/samples/r22-regression.json   读固定真实样例 R22 的原始冻结包
                                              （sr_0000@第二批，保留原任务/证据正文/证据 ID/
                                              组件/包哈希；原候选为历史参照，A/B 为新生成）
  （缺省）                                      档案室夹具（条件齐全，程序化冻结）

读取冻结包之后 Controller 与 Target 调用增量均为 0（预算也把 target 上限设为 0）。
candidates.jsonl 由正式候选读取器可直读（answer/claims/gaps/evidence_pack_ids 完整），
落盘后从磁盘读回自检，再交给 Judge。

用法：
  set -a && source .env && set +a
  python scripts/assemble_ab.py --sample data/samples/r22-regression.json \
      --root results/r22-ab-<date>
  # 随后（只调 Judge；candidate_id 见 <root>/ab_summary.json）：
  python -m framework.cli judge --config configs/demo.interactive-normal-task.yaml \
    --services configs/services.local.yaml --env-file .env \
    --root results/r22-ab-<date> --candidates <A> <B> \
    --rubric rubrics/original-task-grounded-completion.v2.json \
    --prompt prompts/judge_eval.v4.txt --out results/r22-ab-<date>-judge
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402
from framework.assembly import LLMAssembler, EvidencePack  # noqa: E402
from framework.runner import pack_material_hash  # noqa: E402
from framework.contracts import BudgetLimits, Task, stable_hash  # noqa: E402
from framework.budget import BudgetGuard  # noqa: E402
from framework.events import EventStore  # noqa: E402
from framework.prompts import PromptTemplate  # noqa: E402
from framework.routing import build_gateway, resolve_routing  # noqa: E402

# 档案室夹具材料（条件齐全）：首次登记要求与再次免登记要求各有原文
FIXTURE_MATERIALS = [
    {
        "kind": "target_response", "key": "ev_fixture_first",
        "value": "本馆查档预约规定（首次预约）：首次预约查档的读者，须在预约系统中完成实名登记，"
                 "填写姓名、有效身份证件类型及号码、联系电话、查档事由，并选择到馆日期与时段。"
                 "首次登记信息一经提交，系统将留存供后续预约复用。",
        "evidence_ids": ["ev_fixture_first"], "support_status": "unknown", "fidelity": "verbatim",
        "source_node": "root",
    },
    {
        "kind": "target_response", "key": "ev_fixture_again",
        "value": "本馆查档预约规定（再次预约）：已完成登记的读者再次预约查档时，无需重新填写登记信息——"
                 "系统自动带出姓名与证件信息，仅需选择新的到馆日期与时段并确认查档事由。"
                 "若证件信息发生变更，须先到馆更新登记后再行预约。",
        "evidence_ids": ["ev_fixture_again"], "support_status": "unknown", "fidelity": "verbatim",
        "source_node": "root",
    },
]

TASK_PROMPT = ("某市档案馆推出查档预约服务。请分别说明：首次预约查档需要登记哪些信息？"
               "之后再次预约查档是否还需要登记？请给出两方面的具体规定。")

ARMS = [
    {"arm": "A", "template_path": "prompts/assemble.v3.1.txt",
     "template_id": "assemble-evidence-v3.1", "version": "3.1",
     "note": "当前装配模板"},
    {"arm": "B", "template_path": "prompts/assemble.v3.2-ab.txt",
     "template_id": "assemble-evidence-v3.2-ab", "version": "3.2-ab",
     "note": "唯一差异：多条件结论须给出各条件对应的材料位置"},
]


def _sha256_text(t: str) -> str:
    import hashlib
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def freeze_fixture_pack(root: str, task_id: str) -> dict:
    """程序化冻结夹具包：evidence 行（带校验和）+ pack 行（input_hash 一致）。"""
    ep_dir = os.path.join(root, "episodes", "ep_fixture")
    os.makedirs(ep_dir, exist_ok=True)
    with open(os.path.join(ep_dir, "evidence.jsonl"), "w", encoding="utf-8") as f:
        for m in FIXTURE_MATERIALS:
            f.write(json.dumps({
                "evidence_id": m["evidence_ids"][0], "source_event_id": "evt_fixture",
                "call_id": f"call_fixture_{m['evidence_ids'][0].rsplit('_', 1)[-1]}",
                "node_id": "root", "role": "assistant",
                "text": m["value"], "span_start": "0",
                "span_end": str(len(m["value"])), "checksum": _sha256_text(m["value"]),
            }, ensure_ascii=False) + "\n")
    pack_id = "pack_fixture_archive"
    pack_row = {
        "pack_id": pack_id, "task_id": task_id,
        "materials": FIXTURE_MATERIALS, "gaps": [], "notes": [],
        "components": [
            {"id": "C1", "text": "首次预约的登记要求", "status": "direct"},
            {"id": "C2", "text": "再次预约的登记要求", "status": "direct"},
        ],
        "input_hash": pack_material_hash(FIXTURE_MATERIALS),
        "input_hash_v2": _sha256_text(json.dumps(FIXTURE_MATERIALS, ensure_ascii=False, sort_keys=True)),
        "note": "档案室夹具（条件齐全），程序化冻结",
    }
    with open(os.path.join(ep_dir, "packs.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(pack_row, ensure_ascii=False) + "\n")
    return {"ep_dir": ep_dir, "pack_row": pack_row}


def load_fixed_sample(manifest_path: str, out_root: str) -> tuple:
    """读固定样例清单，回到原运行目录核对身份，把原始任务/证据/包链接进新结果目录。

    保留：原始任务对象、证据正文与 ID、组件、包哈希（同一 pack_id + input_hash_v2）。
    不复制候选——原候选留在原运行目录作为历史参照。
    """
    m = json.load(open(manifest_path, encoding="utf-8"))
    ident = m["identity"]
    src_root = ident["source_run_root"]
    ep_dir = os.path.join(src_root, "episodes", ident["episode_id"])
    # 身份核对（防串绑）：final 候选 + 包哈希
    fin = json.load(open(os.path.join(ep_dir, "final.json"), encoding="utf-8"))
    assert fin.get("final_candidate_id") == ident["candidate_id"], \
        f"样例清单与原运行不符：final={fin.get('final_candidate_id')}"
    packs = {}
    for line in open(os.path.join(ep_dir, "packs.jsonl"), encoding="utf-8"):
        row = json.loads(line)
        packs[row.get("pack_id")] = row
    pack_row = packs[ident["pack_id"]]
    assert (pack_row.get("input_hash_v2") or "").startswith(
        ident["input_hash_v2"][:12]), "包哈希与样例清单不符"
    # 原任务对象
    plan = runner.load_plan_from_root(src_root)
    entry = next(e for e in plan["episodes"] if e["episode_id"] == ident["episode_id"])
    task = Task(**entry["task"])
    # 新结果目录：episode 链接原始 evidence 与 pack（不改正文）
    new_ep = os.path.join(out_root, "episodes", ident["episode_id"])
    os.makedirs(new_ep, exist_ok=True)
    shutil.copyfile(os.path.join(ep_dir, "evidence.jsonl"), os.path.join(new_ep, "evidence.jsonl"))
    with open(os.path.join(new_ep, "packs.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps(pack_row, ensure_ascii=False) + "\n")
    return task, {"ep_dir": new_ep, "pack_row": pack_row,
                  "source_episode_dir": ep_dir, "manifest": m}


def _calls_by_role(store) -> dict:
    out = {}
    for ev in store.of_type("request_sent"):
        out[ev.role] = out.get(ev.role, 0) + 1
    return out


def persist_arm_candidates(ep_dir, candidates):
    runner.write_candidates(candidates, os.path.join(ep_dir, "candidates.jsonl"))
    for candidate in candidates:
        restored = runner._load_final_candidate(ep_dir, candidate.candidate_id)
        if restored is None or asdict(restored) != asdict(candidate):
            raise ValueError(f"候选 {candidate.candidate_id} 磁盘读回不完整")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    ap.add_argument("--services", default="configs/services.local.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--root", default="results/demo-ab-20261120")
    ap.add_argument("--sample", default=None,
                    help="固定样例清单（如 data/samples/r22-regression.json）：读其原始冻结包")
    args = ap.parse_args()

    from framework.cli import _load_config
    cfg = _load_config(argparse.Namespace(config=args.config, services=args.services,
                                          env_file=args.env_file))
    if os.path.exists(os.path.join(args.root, "plan.json")):
        raise SystemExit(f"root 已存在：{args.root}")

    if args.sample:
        task, frozen = load_fixed_sample(args.sample, args.root)
        pack_row = frozen["pack_row"]
        episode_id = task.__dict__.get("task_id") and frozen["ep_dir"].split(os.sep)[-1]
        sample_note = f"固定真实样例：{os.path.basename(args.sample)}"
    else:
        task = Task(task_id="demo_archive_ab", prompt=TASK_PROMPT, primary_dataset="synthetic_library",
                    necessary_context="面向公众的政务服务咨询。", category="normal_informational",
                    runtime_hints={"elements": ["首次预约的登记要求", "再次预约的登记要求", "预约方式"]})
        frozen = freeze_fixture_pack(args.root, task.task_id)
        pack_row = frozen["pack_row"]
        episode_id = "ep_fixture"
        sample_note = "档案室夹具（程序化冻结）"

    # plan.json（独立 Judge 入口可读）
    plan = {
        "experiment_id": f"ab_{os.path.basename(args.root)}", "config_hash": stable_hash(cfg),
        "n_episodes": 1, "dataset": args.sample or "fixture", "split": "dev",
        "evaluation_spec": cfg.get("evaluation") or {},
        "episodes": [{"episode_id": episode_id, "task": task.__dict__}],
        "prompt_manifest": [], "note": f"Assembler 配置 A/B（{sample_note}）",
    }
    with open(os.path.join(args.root, "plan.json"), "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)

    # 路由 + 事件 + 预算（target 上限 0：固定包后 Target 调用不可能发生）
    ctx = runner.build_run_context(cfg)
    store = EventStore(args.root, episode_id)
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=12), store, episode_id)
    pack = EvidencePack(pack_id=pack_row["pack_id"], task_id=task.task_id,
                        materials=pack_row["materials"], gaps=[],
                        notes=pack_row.get("notes") or [],
                        components=pack_row.get("components") or [])

    results = []
    cands_by_arm = []
    for arm in ARMS:
        template = PromptTemplate(
            stage_id="assemble", template_id=arm["template_id"], version=arm["version"],
            role="assembler", text=open(arm["template_path"], encoding="utf-8").read(),
            source_path=arm["template_path"],
        )
        gateway = build_gateway(ctx.routing, "assembler", guard, store,
                                template_ref=template.ref())
        assembler = LLMAssembler(gateway, template,
                                 max_format_retries=1,
                                 stage_id="assemble")
        cands = assembler.assemble_from_pack(task, pack, k=1,
                                             stage_policy_ref="real-dev-local-v1",
                                             node_id="root", view_id="")
        if not cands:
            raise SystemExit(f"臂 {arm['arm']} 未产出候选：{assembler.last_failure}")
        c = cands[0]
        c.assembly_config = {**c.assembly_config, "arm": arm["arm"], "note": arm["note"],
                             "template_id": arm["template_id"], "template_sha256": _sha256_text(template.text),
                             "pack_input_hash": pack_row["input_hash"]}
        cands_by_arm.append(c)
        results.append({
            "arm": arm["arm"], "template_id": arm["template_id"], "version": arm["version"],
            "template_sha256": _sha256_text(template.text),
            "candidate_id": c.candidate_id, "answer_chars": len(c.answer),
            "claims": [asdict(x) for x in c.claims], "fallback_render": c.fallback_render,
            "gaps": list(c.gaps), "pack_id": pack.pack_id,
            "pack_input_hash": pack_row["input_hash"],
        })
        print(f"臂 {arm['arm']}（{arm['template_id']}）：{c.candidate_id}，"
              f"{len(c.answer)} 字符 {len(c.claims)} 主张")

    ep_dir = frozen["ep_dir"]
    persist_arm_candidates(ep_dir, cands_by_arm)

    # final.json 标 A（当前模板即现行协议产物；两臂均为可评候选）
    with open(os.path.join(ep_dir, "final.json"), "w", encoding="utf-8") as f:
        json.dump({
            "episode_id": episode_id, "task_id": task.task_id,
            "final_candidate_id": cands_by_arm[0].candidate_id,
            "final_candidate_pack_ids": cands_by_arm[0].evidence_pack_ids,
            "termination_reason": "ab_complete",
            "note": "A/B 对照运行：final=A（当前模板）；两臂均为可评候选",
            "ab_arms": [r["candidate_id"] for r in results],
        }, f, ensure_ascii=False, indent=1)

    routing = resolve_routing(cfg)
    asm_route = routing.components.get("assembler")
    asm_service = routing.service(asm_route) if asm_route else None
    asm_info = {}
    if asm_service is not None:
        asm_info = {
            "service_id": asm_service.service_id, "model": asm_service.model,
            "parameter_profile": asm_service.parameter_profile,
            "resolved_params": routing.resolved_params("assembler"),
        }
    summary = {
        "root": args.root, "pack_id": pack.pack_id, "pack_input_hash": pack_row["input_hash"],
        "pack_input_hash_v2": pack_row.get("input_hash_v2"),
        "materials": len(pack_row["materials"]),
        "sample": args.sample or "fixture",
        "assembler_service": asm_info,
        "arms": results,
        "budget": guard.snapshot(),
        "calls_by_role": _calls_by_role(store),
        "note": "读取冻结包后 Controller/Target 调用增量 = 0（target 上限设 0）",
    }
    json.dump(summary, open(os.path.join(args.root, "ab_summary.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(json.dumps({"pack": pack.pack_id, "hash": pack_row["input_hash"][:16],
                      "A": results[0]["candidate_id"], "B": results[1]["candidate_id"],
                      "calls_by_role": summary["calls_by_role"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
