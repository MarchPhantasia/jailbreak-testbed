"""实验编排与产物（手册 §12、§13、§17）。

目录约定：
results/<exp_id>/
  manifest.json / config.resolved.yaml
  dataset_manifest.json / split_manifest.json / deployment_manifest.json / preflight.json
  episodes/<episode_id>/{events.jsonl,candidates.jsonl,final.json}
  evaluations/<eval_run_id>/{config.json,verdicts.jsonl,metrics.json}
  ledger.json
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any, Dict, Iterable, List, Optional

import yaml

from .contracts import AnswerCandidate, EpisodeOutcome, Task, stable_hash, new_id
from .datasets import get_adapter, load_split, write_json, write_tasks_jsonl
from .judge import JudgeEval, Rubric, compute_metrics, decide_outcome, EpisodeEvaluation
from .methods.independent import EpisodeArtifacts, run_episode


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


def results_root(cfg: Dict[str, Any]) -> str:
    exp = cfg.get("experiment", {}) or {}
    base = exp.get("results_dir", "results")
    return os.path.join(base, exp.get("id", "exp"))


def _subset(tasks: List[Task], limit: Optional[int], offset: int = 0) -> List[Task]:
    if limit is None:
        return tasks[offset:]
    return tasks[offset : offset + limit]


# --------------------------------------------------------------------------
# 数据准备与检查
# --------------------------------------------------------------------------
def prepare_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    path = ds.get("path", "fixtures/synthetic_library/tasks.jsonl")
    seed = int(ds.get("split_seed", cfg.get("experiment", {}).get("seed", 20261110)))
    result, dev, held, manifest = load_split(name, path, seed=seed, dev_ratio=int(ds.get("dev_ratio", 2)))

    out_dir = ds.get("manifests_dir", "data/manifests")
    write_json(manifest.to_dict(), os.path.join(out_dir, f"{name}.dataset_manifest.json"))
    write_json(
        {
            "split_seed": seed,
            "dev": [t.task_id for t in dev],
            "heldout": [t.task_id for t in held],
            "dev_duplicate_groups": sorted({t.duplicate_group_id for t in dev}),
            "heldout_duplicate_groups": sorted({t.duplicate_group_id for t in held}),
        },
        os.path.join(out_dir, f"{name}.split_manifest.json"),
    )
    write_tasks_jsonl(result.tasks, os.path.join(out_dir, f"{name}.tasks.jsonl"))
    return {
        "adapter": name,
        "loaded": len(result.tasks),
        "dev": len(dev),
        "heldout": len(held),
        "excluded": len(result.excluded),
        "raw_sha256": result.raw_sha256,
        "dev_groups": len({t.duplicate_group_id for t in dev}),
        "heldout_groups": len({t.duplicate_group_id for t in held}),
    }


def check_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """固定版本读取、ID 稳定、重复组、开发与测试交集。"""
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    path = ds.get("path", "fixtures/synthetic_library/tasks.jsonl")
    seed = int(ds.get("split_seed", 20261110))
    result, dev, held, manifest = load_split(name, path, seed=seed, dev_ratio=int(ds.get("dev_ratio", 2)))
    dev_ids = {t.task_id for t in dev}
    held_ids = {t.task_id for t in held}
    dev_groups = {t.duplicate_group_id for t in dev}
    held_groups = {t.duplicate_group_id for t in held}
    problems: List[str] = []
    if dev_ids & held_ids:
        problems.append("开发与测试存在相同 task_id")
    if dev_groups & held_groups:
        problems.append("开发与测试存在相同重复组")
    if any(not t.prompt for t in result.tasks):
        problems.append("存在空 prompt")
    return {
        "adapter": name,
        "tasks": len(result.tasks),
        "dev": len(dev),
        "heldout": len(held),
        "groups": len(manifest.duplicate_groups),
        "excluded": result.excluded,
        "ok": not problems,
        "problems": problems,
        "raw_sha256": result.raw_sha256,
    }


# --------------------------------------------------------------------------
# 服务检查（preflight）
# --------------------------------------------------------------------------
def check_services(cfg: Dict[str, Any]) -> Dict[str, Any]:
    target = cfg.get("target", {}) or {}
    kind = target.get("type", "mock")
    if kind == "mock":
        return {"target": {"type": "mock", "ok": True, "note": "不联网，不产生 served 调用"}}
    base = target.get("base_url", "")
    ok = bool(base)
    return {
        "target": {
            "type": kind,
            "base_url": base,
            "ok": ok,
            "note": "只做客户端可达性检查；不用于估计 Target 拒答行为",
        }
    }


# --------------------------------------------------------------------------
# 运行
# --------------------------------------------------------------------------
def _write_candidates(artifacts: EpisodeArtifacts, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for c in artifacts.candidates():
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")


def write_final(artifacts: EpisodeArtifacts, path: str, outcome: Optional[str] = None, verdict_id: Optional[str] = None, basis: str = "") -> Dict[str, Any]:
    payload = {
        "episode_id": artifacts.episode_id,
        "task_id": artifacts.task.task_id,
        "outcome": outcome,
        "verdict_id": verdict_id,
        "termination_reason": artifacts.termination_reason,
        "final_candidate_id": artifacts.final_candidate.candidate_id if artifacts.final_candidate else None,
        "steps": artifacts.steps,
        "ledger": artifacts.store.ledger(),
        "basis": basis,
    }
    write_json(payload, path)
    return payload


def run_experiment(cfg: Dict[str, Any], *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    root = results_root(cfg)
    os.makedirs(root, exist_ok=True)
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    path = ds.get("path", "fixtures/synthetic_library/tasks.jsonl")
    seed = int(ds.get("split_seed", cfg.get("experiment", {}).get("seed", 20261110)))
    result, dev, held, manifest = load_split(name, path, seed=seed, dev_ratio=int(ds.get("dev_ratio", 2)))
    pool = dev if ds.get("split", "dev") == "dev" else held
    tasks = _subset(pool, limit if limit is not None else ds.get("limit"), offset)

    write_json(
        {
            "experiment_id": cfg.get("experiment", {}).get("id"),
            "config_hash": stable_hash(cfg),
            "dataset": name,
            "split": ds.get("split", "dev"),
            "n_tasks": len(tasks),
        },
        os.path.join(root, "manifest.json"),
    )
    with open(os.path.join(root, "config.resolved.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=True)
    write_json(manifest.to_dict(), os.path.join(root, "dataset_manifest.json"))
    write_json(check_services(cfg), os.path.join(root, "preflight.json"))
    write_json(cfg.get("deployment", {}) or {"note": "部署由研究者提供；此处只记录可得信息"}, os.path.join(root, "deployment_manifest.json"))

    artifacts_list: List[EpisodeArtifacts] = []
    for task in tasks:
        art = run_episode(task, config=cfg, results_root=root)
        ep_dir = os.path.join(root, "episodes", art.episode_id)
        os.makedirs(ep_dir, exist_ok=True)
        _write_candidates(art, os.path.join(ep_dir, "candidates.jsonl"))
        write_final(art, os.path.join(ep_dir, "final.json"))
        artifacts_list.append(art)

    ledgers = [a.store.ledger() for a in artifacts_list]
    summary = {
        "experiment_id": cfg.get("experiment", {}).get("id"),
        "episodes": len(artifacts_list),
        "target_calls": [l["target_calls"] for l in ledgers],
        "ledger_total": {
            "target_calls": sum(l["target_calls"] for l in ledgers),
            "served_calls": sum(l["served_calls"] for l in ledgers),
            "cache_hits": sum(l["cache_hits"] for l in ledgers),
            "tokens_in": sum(l["tokens_in"] for l in ledgers),
            "tokens_out": sum(l["tokens_out"] for l in ledgers),
        },
    }
    write_json(summary, os.path.join(root, "ledger.json"))
    return summary


# --------------------------------------------------------------------------
# 评价（独立运行；不重跑 Target）
# --------------------------------------------------------------------------
def evaluate_experiment(cfg: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    root = root or results_root(cfg)
    judge_cfg = (cfg.get("method", {}) or {}).get("judge", {}) or {}
    elements = judge_cfg.get("rubric_elements") or []
    rubric = Rubric(
        rubric_id=judge_cfg.get("rubric_id", "inline"),
        elements=elements,
        completion_threshold=float(judge_cfg.get("completion_threshold", 0.6)),
        full_threshold=float(judge_cfg.get("full_threshold", 0.8)),
        version=str(judge_cfg.get("version", "v1")),
    )
    eval_run_id = new_id("eval")
    eval_dir = os.path.join(root, "evaluations", eval_run_id)
    os.makedirs(eval_dir, exist_ok=True)
    write_json({"rubric": asdict(rubric), "judge_ref": "rule@v1"}, os.path.join(eval_dir, "config.json"))

    judge = JudgeEval(rubric)
    evaluations: List[EpisodeEvaluation] = []
    episodes_dir = os.path.join(root, "episodes")
    for ep_id in sorted(os.listdir(episodes_dir)) if os.path.isdir(episodes_dir) else []:
        ep_dir = os.path.join(episodes_dir, ep_id)
        final_path = os.path.join(ep_dir, "final.json")
        if not os.path.exists(final_path):
            continue
        final = json.load(open(final_path, "r", encoding="utf-8"))
        task, ep_elements = _load_task_from_events(ep_dir)
        if ep_elements:
            rubric = Rubric(
                rubric_id=rubric.rubric_id,
                elements=[{"key": e, "critical": True, "accept": [e]} for e in ep_elements],
                completion_threshold=rubric.completion_threshold,
                full_threshold=rubric.full_threshold,
                version=rubric.version,
            )
            judge = JudgeEval(rubric)
        candidate = _load_final_candidate(ep_dir, final.get("final_candidate_id"))
        verdict = judge.evaluate(task, candidate, eval_run_id)
        failure_established = candidate is None
        ev = decide_outcome(
            episode_id=ep_id,
            task_id=task.task_id,
            final_answer=candidate,
            verdict=verdict,
            failure_established=failure_established,
        )
        evaluations.append(ev)
        with open(os.path.join(eval_dir, "verdicts.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(verdict), ensure_ascii=False) + "\n")
        final["outcome"] = ev.outcome.value
        final["verdict_id"] = verdict.verdict_id
        final["basis"] = ev.reason
        write_json(final, final_path)

    metrics = compute_metrics(evaluations)
    write_json(metrics, os.path.join(eval_dir, "metrics.json"))
    return {"eval_run_id": eval_run_id, "dir": eval_dir, **metrics}


def _load_task_from_events(ep_dir: str):
    """从 episode_started 事件恢复 task 身份与要求要素（评价只需这两样）。"""
    for line in open(os.path.join(ep_dir, "events.jsonl"), "r", encoding="utf-8"):
        ev = json.loads(line)
        if ev["type"] == "episode_started":
            p = ev.get("payload", {})
            return (
                Task(task_id=p.get("task_id", "unknown"), prompt=p.get("prompt", ""), primary_dataset="unknown"),
                list(p.get("elements") or []),
            )
    return Task(task_id="unknown", prompt="", primary_dataset="unknown"), []


def _load_final_candidate(ep_dir: str, candidate_id: Optional[str]) -> Optional[AnswerCandidate]:
    path = os.path.join(ep_dir, "candidates.jsonl")
    if not candidate_id or not os.path.exists(path):
        return None
    for line in open(path, "r", encoding="utf-8"):
        d = json.loads(line)
        if d.get("candidate_id") == candidate_id:
            return AnswerCandidate(**{k: v for k, v in d.items() if k in AnswerCandidate.__dataclass_fields__})
    return None


# --------------------------------------------------------------------------
# 恢复与重算（不调用模型）
# --------------------------------------------------------------------------
def restore_episode(ep_dir: str) -> Dict[str, Any]:
    from .events import EventStore

    ep_id = os.path.basename(ep_dir.rstrip("/"))
    root = os.path.dirname(os.path.dirname(ep_dir))
    store = EventStore(root, ep_id)
    return {"episode_id": ep_id, "events": len(store.events), "ledger": store.ledger(), "terminated": bool(store.terminations())}


def analyze_nodes(ep_dir: str) -> Dict[str, Any]:
    """节点级归因分析的骨架：统计每个节点的证据条数与是否被后续装配引用。"""
    records = []
    for line in open(os.path.join(ep_dir, "events.jsonl"), "r", encoding="utf-8"):
        ev = json.loads(line)
        if ev["type"] == "state_write":
            records.append({"seq": ev["seq"], "new_facts": len(ev["payload"].get("new_facts", [])), "node": ev.get("node_id")})
    return {"episode": os.path.basename(ep_dir.rstrip("/")), "nodes": records, "note": "节点级干预需要重跑装配，属 analysis 成本"}
