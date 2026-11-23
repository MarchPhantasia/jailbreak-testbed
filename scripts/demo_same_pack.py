#!/usr/bin/env python
"""M2 同包双候选对照（普通资料任务，真实服务）。

驱动 InteractiveSession 完成：两次 Target 查询 → k=2 装配（同一冻结包出两份候选）
→ 收尾落盘。产物可直接被独立评价入口（framework.cli judge --candidates）双评。

用法：
  set -a && source .env && set +a
  python scripts/demo_same_pack.py --root results/demo-same-pack-20261120

随后双评（只调 Judge，零生成调用）：
  python -m framework.cli judge --config configs/demo.interactive-normal-task.yaml \
    --services configs/services.local.yaml --env-file .env \
    --root results/demo-same-pack-20261120 \
    --candidates <cand1> <cand2> \
    --rubric rubrics/original-task-grounded-completion.v2.json \
    --prompt prompts/judge_eval.v4.txt \
    --out results/demo-same-pack-20261120-judge --judge-stream
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402
from framework.interactive import run_scripted  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    ap.add_argument("--services", default="configs/services.local.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--root", default="results/demo-same-pack-20261120")
    ap.add_argument("--edit-step", type=int, default=0,
                    help="在第 N 次 approve 时附加演示编辑（0=不编辑）")
    args = ap.parse_args()

    from framework.cli import _load_config  # 复用 env+overlay 加载
    cfg = _load_config(argparse.Namespace(config=args.config, services=args.services,
                                          env_file=args.env_file))
    if os.path.exists(os.path.join(args.root, "plan.json")):
        raise SystemExit(f"root 已存在：{args.root}（换一个）")

    # 从配置的数据集取第一条任务（与 build_plan 同一路径）
    from framework.datasets import load_split
    from framework.runner import resolve_path
    ds = cfg.get("datasets", {}) or {}
    path = resolve_path(cfg, str(ds.get("path")))
    result, dev, held, manifest = load_split(
        ds.get("adapter", "synthetic_library"), path,
        seed=int(ds.get("split_seed", 20261110)), dev_ratio=int(ds.get("dev_ratio", 2)),
        revision=ds.get("revision"),
    )
    pool = dev if ds.get("split", "dev") == "dev" else held
    tasks = pool[: int(ds.get("limit", 1) or 1)]
    task = tasks[0]
    print(f"任务 {task.task_id}: {task.prompt[:60]}…")

    script = [
        {"op": "step"},
        {"op": "approve"},
        {"op": "step"},
        {"op": "approve"},
        {"op": "step"},
        {"op": "approve"},
        {"op": "assemble", "reason": "elements_covered_demo"},
        {"op": "finalize", "reason": "demo_same_pack_done"},
    ]
    if args.edit_step:
        script.insert(args.edit_step, {"op": "approve",
                                       "edit": "请分别说明首次预约查档与再次预约查档的登记要求，逐项列出。",
                                       "reason": "演示：人工把问题改得更明确"})

    out = run_scripted(cfg, task, args.root, script)
    sess = out["session"]
    st = sess.state()
    summary = {
        "root": args.root,
        "episode_id": st["episode_id"],
        "steps": st["steps"],
        "budget": st["budget"],
        "nodes": len(st["nodes"]),
        "packs": st["packs"],
        "candidates": st["candidates"],
        "final_candidate": st["final_candidate"],
        "log": out["log"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    json.dump(summary, open(os.path.join(args.root, "demo_same_pack.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    cids = [c["candidate_id"] for c in st["candidates"]]
    print(f"\n双评命令的 --candidates 参数：{' '.join(cids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
