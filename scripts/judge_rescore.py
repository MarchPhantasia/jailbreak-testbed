#!/usr/bin/env python
"""冻结候选的裁判复评（k 次同输入盲评）：分离裁判波动与真实翻转。

协议（外部方法论建议）：
- 固定实际发送的消息、证据范围与顺序、模板、模型与参数（与原判定同一 v2 模板）；
- 逐字重建校验：重建请求与原判定 events 中的 request_sent 消息做 sha256 对比
  （按任务原文子串定位原请求），仅 request_match=True 的记录计入正式复评；
- 随机交错送评（seed 记录在产物中），独立调用（新网关无缓存）；
- 不向裁判展示批次信息与原判定（输入天然只含任务+候选+证据）；
- 原主终点不变；本产物是稳定性分析，k 次投票不替换原判。

用法：
  python scripts/judge_rescore.py --root results/<id> [--root results/<id2>] \
      --config configs/<同判据配置>.yaml --services configs/services.local.yaml \
      --env-file .env --k 2
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
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from framework import runner  # noqa: E402
from framework.budget import BudgetGuard  # noqa: E402
from framework.cli import _load_config  # noqa: E402
from framework.contracts import Task, new_id  # noqa: E402
from framework.evaluation import SemanticJudge  # noqa: E402
from framework.events import EventStore  # noqa: E402
from framework.prompts import PromptTemplate  # noqa: E402
from framework.routing import build_gateway  # noqa: E402


def sha(m) -> str:
    return hashlib.sha256(json.dumps(m, ensure_ascii=False, sort_keys=False).encode("utf-8")).hexdigest()


def collect(root: str):
    """收集该 root 内可复评的（task, episode, final_candidate, packs）。"""
    plan = runner.load_plan_from_root(root)
    spec = plan["evaluation_spec"] or {}
    recs = []
    for e in plan["episodes"]:
        ep = os.path.join(root, "episodes", e["episode_id"])
        if not os.path.isdir(ep) or not os.path.exists(os.path.join(ep, "final.json")):
            continue
        fin = json.load(open(os.path.join(ep, "final.json"), encoding="utf-8"))
        cid = fin.get("final_candidate_id")
        if not cid or not os.path.exists(os.path.join(ep, "candidates.jsonl")):
            continue
        cand = runner._load_final_candidate(ep, cid)
        if cand is None:
            continue
        recs.append({
            "root": root, "task_def": e["task"], "episode_dir": ep,
            "candidate": cand, "spec": spec,
        })
    return recs


def original_requests(root: str):
    """(task_prompt_substring -> messages) 映射，取该 root 全部 eval 目录的 request_sent。"""
    out = {}
    for ev in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "events.jsonl"))):
        for l in open(ev, encoding="utf-8"):
            e = json.loads(l)
            if e["type"] != "request_sent":
                continue
            msgs = e["payload"]["messages"]
            out.setdefault(sha(msgs), msgs)
    return list(out.values())


def original_verdicts(root: str):
    out = {}
    for vp in sorted(glob.glob(os.path.join(root, "evaluations", "eval_*", "verdicts.jsonl"))):
        for l in open(vp, encoding="utf-8"):
            v = json.loads(l)
            if v.get("cls"):
                out[v["task_id"]] = v  # 后读的 eval 覆盖（最新）
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--services", default=None)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--k", type=int, default=2, help="每个候选的新增盲评次数")
    ap.add_argument("--seed", type=int, default=20261118)
    ap.add_argument("--budget", type=int, default=150)
    ap.add_argument("--out-dir", default="")
    args = ap.parse_args()

    cfg = _load_config(SimpleNamespace(config=args.config, services=args.services, env_file=args.env_file))
    probe_id = new_id("rescore")
    out_dir = args.out_dir or f"results/judge-rescore_{probe_id}"
    os.makedirs(out_dir, exist_ok=True)

    # 每个 root 用其冻结 spec 构造同构 judge（模板/参数与原判定一致；仅预算覆盖为本次复评量）
    judges = {}
    recs = []
    verdicts = {}
    orig_msgs = []
    for root in args.root:
        plan = runner.load_plan_from_root(root)
        spec = dict(plan["evaluation_spec"] or {})
        spec["budget"] = {"non_target_calls": args.budget}
        eval_dir = os.path.join(root, "evaluations", f"eval_{probe_id}")
        judge, _store, _g, _gw = runner._build_eval_judge(eval_dir, plan, spec, probe_id)
        judges[root] = judge
        recs.extend(collect(root))
        verdicts.update(original_verdicts(root))
        orig_msgs.extend(original_requests(root))
    orig_hashes = {sha(m) for m in orig_msgs}
    rng = random.Random(args.seed)
    rng.shuffle(recs)

    results = []
    for i, rec in enumerate(recs):
        tid = rec["task_def"]["task_id"]
        task = Task(**rec["task_def"])
        episode_evidence = runner._read_evidence(rec["episode_dir"], None)
        evidence, scope_info = runner.candidate_evidence_scope(
            rec["episode_dir"], rec["candidate"], episode_evidence,
            mode=str((rec["spec"].get("evidence_scope")) or "candidate_pack"),
        )
        # 逐字重建校验
        try:
            payload, messages, _est, _disp = judge.prepare_request(task, rec["candidate"], evidence)
            request_match = sha(messages) in orig_hashes
        except Exception as exc:  # noqa: BLE001
            results.append({"task": tid, "error": f"prepare_failed:{exc}"})
            continue
        new_verdicts = []
        for j in range(args.k):
            v = judges[rec["root"]].evaluate(task, rec["candidate"], eval_run_id=probe_id,
                                             evidence=evidence, rubric_version="v1")
            new_verdicts.append({"cls": v.cls, "basis": v.basis, "verdict_id": v.verdict_id})
        o = verdicts.get(tid, {})
        results.append({
            "task": tid, "request_match": request_match,
            "original": {"cls": o.get("cls"), "basis": o.get("basis")},
            "new": new_verdicts,
        })
        print(f"[{i+1}/{len(recs)}] {tid} match={request_match} orig={o.get('cls')} "
              f"new={[v['cls'] for v in new_verdicts]}", flush=True)

    # 聚合
    def dims(b):
        d = {}
        for part in (b or "").split(","):
            k, _, val = part.strip().removeprefix("semantic:").partition("=")
            if k in ("tf", "src", "ev", "success"):
                d[k] = val
        return d

    agg = {"judged": 0, "request_match": 0, "cls_disagree_tasks": 0, "dim_flips": {},
           "success_flips": 0}
    for r in results:
        if "error" in r or not r.get("new"):
            continue
        agg["judged"] += 1
        agg["request_match"] += bool(r["request_match"])
        o = r["original"]
        ocls, odims = (o or {}).get("cls"), dims((o or {}).get("basis"))
        disagree = False
        for nv in r["new"]:
            nd = dims(nv["basis"])
            for k in ("tf", "src", "ev"):
                if odims.get(k) and nd.get(k) and odims[k] != nd[k]:
                    agg["dim_flips"][k] = agg["dim_flips"].get(k, 0) + 1
            if nv["cls"] != ocls:
                disagree = True
            if odims.get("success") != nd.get("success"):
                agg["success_flips"] += 1
        agg["cls_disagree_tasks"] += disagree

    out = {"probe_id": probe_id, "k": args.k, "seed": args.seed, "template": judges[args.root[0]].judge_ref,
           "roots": args.root, "aggregate": agg, "results": results}
    with open(os.path.join(out_dir, "rescore.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(json.dumps({"out_dir": out_dir, **agg}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
