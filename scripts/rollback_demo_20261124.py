#!/usr/bin/env python
"""人工回退验收（2026-11-24 方案 §5.2）：同工单任务、单交互会话、真实模型。

两步正常推进 → 从"只见过第 1 次回复"的观察节点人工开分支（branch_started 留痕）
→ 分支上执行下一动作 → 装配收尾。保存起点、下一动作与最终候选 ID。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from framework.envfile import load_env_file  # noqa: E402
from framework.interactive import InteractiveSession  # noqa: E402
from framework.runner import load_config  # noqa: E402

load_env_file(str(ROOT / ".env"))


def main() -> int:
    cfg = load_config(str(ROOT / "configs" / "real-dev.ticket-movie-20261124.yaml"))
    # 服务覆盖：与正式 run 入口一致（否则用 YAML 占位地址 127.0.0.1:9 → 拒连）
    from framework.runner import apply_service_overlay
    apply_service_overlay(cfg, str(ROOT / "configs" / "services.local.yaml"))
    row = json.loads(open(ROOT / "data/slices/ticket-public-movie-access-001.jsonl",
                          encoding="utf-8").readline())
    from framework.contracts import Task
    task = Task(task_id=row["task_id"], prompt=row["prompt"],
                primary_dataset="synthetic_library",
                necessary_context=row.get("context", ""),
                category=row.get("category", "ticket"),
                runtime_hints={"elements": row.get("elements", [])})
    root = str(ROOT / "results" / "ticket-movie-rollback-20261124")
    sess = InteractiveSession(cfg, task, root)
    log = []

    def do(fn, *a, **k):
        out = fn(*a, **k)
        log.append(out)
        print("→", json.dumps({k2: v for k2, v in out.items()
                               if k2 in ("status", "reason", "action", "node", "from_node",
                                         "final_candidate_id", "query", "note")},
                              ensure_ascii=False)[:220])
        return out

    # 两步正常推进
    for i in range(2):
        do(sess.step)
        do(sess.approve)
    nodes = sorted(sess.engine.nodes)
    obs = [n for n in nodes if n != "root"]
    pick = obs[0] if obs else "root"
    print(f"现有节点: {nodes}；回退点选: {pick}（只见过第1次回复的观察节点）")
    # 人工回退：从旧节点开分支
    do(sess.branch_from, pick,
       "人工回退演示：第2次回复显示渠道依赖图书馆订阅，回到只见过第1次回复的节点，"
       "让 Controller 重新决定下一步（核实公开服务条件方向）")
    # 分支上的下一动作 ×2 + 装配
    do(sess.step)
    do(sess.approve)
    do(sess.step)
    fin = do(sess.assemble_now, "rollback_branch_assembled")
    out = do(sess.finalize, "rollback_demo_done")
    final_id = out.get("final_candidate_id") or fin.get("candidate_id")
    record = {
        "root": root, "nodes_before_rollback": nodes, "rollback_node": pick,
        "branch_note": "人工回退：回到第1次回复后的观察节点",
        "next_action_after_branch": log[-4] if len(log) >= 4 else None,
        "final_candidate_id": final_id,
        "steps": [ {k: v for k, v in l.items() if isinstance(v, (str, int, float, bool, type(None)))}
                   for l in log],
    }
    json.dump(record, open(str(ROOT / "results" / "ticket-movie-rollback-20261124-record.json"),
                           "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("记录:", ROOT / "results/ticket-movie-rollback-20261124-record.json")
    print("最终候选:", final_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
