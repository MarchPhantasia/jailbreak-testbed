#!/usr/bin/env python
"""M3 交互演示：普通资料任务的人工分支（暂停→编辑→开分支→双轨迹→装配）。

流程（真实服务，预算受 BudgetGuard 约束）：
  1. 控制器提议查询 1 → 原样批准；
  2. 控制器提议查询 2 → 人工编辑后批准（记录 query_edited：原/改/理由）；
  3. 从查询 1 的观察节点开分支（记录 branch_started），在分支上继续一问；
  4. 分支上装配 k=2 → 收尾。
产物与在线运行同格式，网页回放可见两条轨迹与各自消耗。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from framework import runner  # noqa: E402
from framework.interactive import InteractiveSession  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/demo.interactive-normal-task.yaml")
    ap.add_argument("--services", default="configs/services.local.yaml")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--root", default="results/demo-branch-20261120")
    args = ap.parse_args()

    from framework.cli import _load_config
    cfg = _load_config(argparse.Namespace(config=args.config, services=args.services,
                                          env_file=args.env_file))
    if os.path.exists(os.path.join(args.root, "plan.json")):
        raise SystemExit(f"root 已存在：{args.root}")

    from framework.datasets import load_split
    from framework.runner import resolve_path
    ds = cfg.get("datasets", {}) or {}
    path = resolve_path(cfg, str(ds.get("path")))
    result, dev, held, manifest = load_split(
        ds.get("adapter", "synthetic_library"), path,
        seed=int(ds.get("split_seed", 20261110)), dev_ratio=int(ds.get("dev_ratio", 2)),
        revision=ds.get("revision"))
    task = (dev if ds.get("split", "dev") == "dev" else held)[0]
    print(f"任务 {task.task_id}: {task.prompt[:60]}…")

    sess = InteractiveSession(cfg, task, args.root)
    log: list = []

    # 1) 查询 1：原样批准
    out = sess.step(); log.append(out)
    assert out["status"] == "pending_approval", out
    q1_node = out["action"]["node_id"]
    log.append(sess.approve())
    print("查询 1 已发送")

    # 2) 查询 2：人工编辑后批准
    out = sess.step(); log.append(out)
    if out["status"] == "pending_approval":
        original_q = out["action"]["query"]
        edited_q = ("请重点说明：如果我已经完成过一次预约登记，第二次预约查档还需要重新登记吗？"
                    "请给出需要与不需要两种情形的条件。")
        log.append(sess.approve(edited_query=edited_q,
                                reason="演示：把第二次预约的登记条件问得更明确"))
        print(f"查询 2 已编辑后发送（原 {len(original_q)} 字 → 改 {len(edited_q)} 字）")

    # 3) 从查询 1 的观察节点开分支
    st = sess.state()
    # 找 root 的子节点中 materials>0 的观察节点（查询 1 的回复所在）
    branch_base = ""
    for n in st["nodes"]:
        if n["parent"] == "root" and n["materials"] > 0:
            branch_base = n["node_id"]
            break
    assert branch_base, "找不到查询 1 的观察节点"
    log.append(sess.branch_from(branch_base, note="演示：回到第一次查询的回复处，换一个方向继续"))
    print(f"已从 {branch_base} 开分支")

    # 4) 分支上再问一步（原样批准）
    out = sess.step(); log.append(out)
    if out["status"] == "pending_approval":
        log.append(sess.approve())
        print("分支查询已发送")

    # 5) 分支上装配 + 收尾
    log.append(sess.assemble_now("branch_demo_assemble"))
    log.append(sess.finalize("branch_demo_done"))
    final = sess.state()
    summary = {
        "root": args.root, "episode_id": final["episode_id"],
        "steps": final["steps"], "budget": final["budget"],
        "nodes": final["nodes"], "packs": final["packs"],
        "candidates": final["candidates"],
        "final_candidate": final["final_candidate"],
        "branch_base": branch_base,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    json.dump(summary, open(os.path.join(args.root, "demo_branch.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
