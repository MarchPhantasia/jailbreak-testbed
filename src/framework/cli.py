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
from .envfile import EnvFileError, load_env_if_configured


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_config(args: argparse.Namespace) -> Dict[str, Any]:
    """先加载可选的 env 文件（显式/`JBT_ENV_FILE`），再读配置，最后套用可选的 --services 覆盖。"""
    try:
        info = load_env_if_configured(getattr(args, "env_file", None))
    except EnvFileError as exc:
        raise SystemExit(f"env 文件加载失败：{exc}")
    if info:
        # 只报告键名与统计，不回显任何值
        print(
            json.dumps(
                {
                    "env_file": info["path"],
                    "loaded_keys": info["loaded_keys"],
                    "skipped_existing_keys": info["skipped_existing_keys"],
                    "permissions": info["permissions"],
                    **({"warning": info["warning"]} if info["warning"] else {}),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    cfg = runner.load_config(args.config)
    overlay_path = getattr(args, "services", None)
    if overlay_path:
        try:
            info = runner.apply_service_overlay(cfg, overlay_path)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"服务覆盖文件加载失败：{exc}")
        print(
            json.dumps(
                {
                    "services_overlay": info["path"],
                    "services_overlay_sha256": info["sha256"],
                    "replaced_service_ids": info["replaced_service_ids"],
                    "added_service_ids": info["added_service_ids"],
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
    return cfg


def cmd_prepare_data(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    out = runner.prepare_data(cfg)
    _print({"command": "prepare-data", "calls_model": False, **out})
    return 0


def cmd_check_data(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    out = runner.check_data(cfg)
    _print({"command": "check-data", "calls_model": False, **out})
    return 0 if out["ok"] else 1


def cmd_show_config(args: argparse.Namespace) -> int:
    """展示最终解析的角色→服务→参数，不调用模型。"""
    cfg = _load_config(args)
    try:
        out = runner.describe_routes(cfg)
    except (ValueError, RuntimeError) as exc:
        _print({"command": "show-config", "ok": False, "error": str(exc)})
        return 1
    _print(out)
    return 0


def cmd_check_services(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    out = runner.check_services(cfg)
    _print({"command": "check-services", "calls_model": True, "phase": "preflight", **out})
    # 不可达/未生成时非零退出（探针成本单独记账，不进 episode 预算）
    return 0 if out.get("healthy") else 1


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    if args.limit is not None:
        cfg.setdefault("datasets", {})["limit"] = args.limit
    try:
        out = runner.run_experiment(cfg, offset=args.offset)
    except FileExistsError as exc:
        _print({"command": "run", "ok": False, "error": str(exc), "hint": "换 experiment.id 或换 results_dir 再跑"})
        return 2
    except (RuntimeError, ValueError) as exc:
        _print({"command": "run", "ok": False, "error": str(exc), "phase": "preflight_or_validation"})
        return 1
    _print({"command": "run", "calls_model": True, "phase": "online", **out})
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    cfg = _load_config(args)
    root = args.root or runner.results_root(cfg)
    try:
        spec = runner.load_plan_from_root(root).get("evaluation_spec") or {"type": "rule"}
    except FileNotFoundError:
        spec = {"type": "rule"}
    try:
        out = runner.evaluate_experiment(cfg, root=args.root, eval_service=getattr(args, "eval_service", None))
    except ValueError as exc:
        _print({"command": "evaluate", "ok": False, "error": str(exc), "phase": "validation"})
        return 1
    _print({"command": "evaluate", "calls_model": spec.get("type") == "llm", "phase": "analysis", **out})
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out = runner.report_run(args.root)
    if args.csv:
        import csv

        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            fields = ["episode_id", "task_id", "outcome", "basis", "verdict_id", "class", "judge_ref", "fallback_render"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for row in out["per_episode"]:
                writer.writerow({k: row.get(k) for k in fields})
        out["csv"] = args.csv
    _print({"command": "report", "calls_model": False, "phase": "analysis", **{k: out[k] for k in ("experiment_id", "episodes", "eval_run_id", "metrics", "calls_by_role", "calls_by_stage")}})
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
        ("show-config", cmd_show_config, "展示最终角色路由与发送参数（不调用模型）"),
        ("run", cmd_run, "执行 episode（会调用模型/模拟服务）"),
        ("evaluate", cmd_evaluate, "对已保存答案执行 Judge_eval（不重跑 Target）"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--config", required=True, help="实验配置 yaml")
        sp.add_argument(
            "--env-file",
            default=None,
            help="可选的 KEY=VALUE 文件（如 .env）；系统环境优先，只报告键名",
        )
        sp.add_argument(
            "--services",
            default=None,
            help="可选的本机服务文件（只含顶层 services）；按 service ID 整项替换实验 YAML 的同名服务",
        )
        if name == "run":
            sp.add_argument("--limit", type=int, default=None)
            sp.add_argument("--offset", type=int, default=0)
        if name == "evaluate":
            sp.add_argument("--root", default=None)
            sp.add_argument(
                "--eval-service",
                default=None,
                help="旧实验重评时的裁判连接迁移目标服务 ID（只替换连接层，不改实验条件）",
            )
        sp.set_defaults(func=fn)

    sp = sub.add_parser("report", help="导出逐题结果与汇总（不调用模型）")
    sp.add_argument("--root", required=True, help="results/<experiment_id>")
    sp.add_argument("--csv", default="", help="额外写出逐题 CSV")
    sp.set_defaults(func=cmd_report)

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
