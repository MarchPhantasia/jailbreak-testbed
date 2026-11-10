"""命令行（手册 §17）。

每个命令都明确：是否调用模型、用哪份预算、是否产生新运行 ID。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from . import runner
from .datasets import get_adapter, write_json


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_prepare_data(args: argparse.Namespace) -> int:
    cfg = runner.load_config(args.config)
    out = runner.prepare_data(cfg)
    _print({"command": "prepare-data", "calls_model": False, **out})
    return 0


def cmd_check_data(args: argparse.Namespace) -> int:
    cfg = runner.load_config(args.config)
    out = runner.check_data(cfg)
    _print({"command": "check-data", "calls_model": False, **out})
    return 0 if out["ok"] else 1


def cmd_check_services(args: argparse.Namespace) -> int:
    cfg = runner.load_config(args.config)
    out = runner.check_services(cfg)
    _print({"command": "check-services", "calls_model": False, "phase": "preflight", **out})
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    cfg = runner.load_config(args.config)
    if args.limit is not None:
        cfg.setdefault("datasets", {})["limit"] = args.limit
    out = runner.run_experiment(cfg, offset=args.offset)
    _print({"command": "run", "calls_model": True, "phase": "online", **out})
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    cfg = runner.load_config(args.config)
    out = runner.evaluate_experiment(cfg, root=args.root)
    _print({"command": "evaluate", "calls_model": False, "phase": "analysis", **out})
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    out = runner.restore_episode(args.episode_dir)
    _print({"command": "restore", "calls_model": False, "phase": "analysis", **out})
    return 0


def cmd_recompute_state(args: argparse.Namespace) -> int:
    """派生运行：不覆盖旧状态，产出新的运行 ID 目录。"""
    src = args.episode_dir.rstrip("/")
    ep_id = os.path.basename(src)
    root = os.path.dirname(os.path.dirname(src))
    from .contracts import new_id

    derived_id = new_id("recompute")
    dst = os.path.join(root, "episodes", f"{ep_id}__{derived_id}")
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(src, "events.jsonl"), "r", encoding="utf-8") as f:
        content = f.read()
    with open(os.path.join(dst, "events.jsonl"), "w", encoding="utf-8") as f:
        f.write(content)
    write_json(
        {"derived_from": ep_id, "derived_id": derived_id, "note": "state 重算需要新抽取器配置；本命令只建立派生目录，不覆盖旧状态"},
        os.path.join(dst, "derived.json"),
    )
    _print({"command": "recompute-state", "calls_model": False, "derived_dir": dst})
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """离线审计骨架：统计证据、状态写入与未评估项，不触发模型调用。"""
    ep_dir = args.episode_dir
    counts: Dict[str, int] = {}
    events_path = os.path.join(ep_dir, "events.jsonl")
    with open(events_path, "r", encoding="utf-8") as f:
        for line in f:
            ev = json.loads(line)
            counts[ev["type"]] = counts.get(ev["type"], 0) + 1
    out = {"command": "audit", "calls_model": False, "phase": "analysis", "episode": os.path.basename(ep_dir.rstrip("/")), "event_counts": counts}
    _print(out)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    out = runner.analyze_nodes(args.episode_dir)
    _print({"command": "analyze", "calls_model": False, "phase": "analysis", **out})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="framework", description="独立越狱研究框架 v0.1")
    sub = p.add_subparsers(dest="command", required=True)

    for name, fn, help_text in (
        ("prepare-data", cmd_prepare_data, "生成数据清单与划分（不调用模型）"),
        ("check-data", cmd_check_data, "校验清单、重复组与划分交集（不调用模型）"),
        ("check-services", cmd_check_services, "客户端可达性检查（preflight，单独记账）"),
        ("run", cmd_run, "执行 episode（会调用模型/模拟服务）"),
        ("evaluate", cmd_evaluate, "对已保存答案执行 Judge_eval（不重跑 Target）"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--config", required=True, help="实验配置 yaml")
        if name == "run":
            sp.add_argument("--limit", type=int, default=None)
            sp.add_argument("--offset", type=int, default=0)
        if name == "evaluate":
            sp.add_argument("--root", default=None)
        sp.set_defaults(func=fn)

    for name, fn, help_text in (
        ("restore", cmd_restore, "从事件恢复 episode（不调用模型）"),
        ("recompute-state", cmd_recompute_state, "建立派生运行目录（不覆盖旧状态）"),
        ("audit", cmd_audit, "离线审计事件计数（不调用模型）"),
        ("analyze", cmd_analyze, "节点级分析骨架（不调用模型）"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--episode-dir", required=True)
        sp.set_defaults(func=fn)
    return p


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
