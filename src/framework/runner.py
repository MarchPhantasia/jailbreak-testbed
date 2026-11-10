"""实验编排与产物（手册 §12、§13、§17）。

修复要点（review a9b45c9）：
- R02：每题 rubric 在计划阶段冻结并落盘；评价只读冻结对象，不重建 accept/critical；
- R03：计划先冻结 episode 清单，评价按清单对账；同 ID 重跑拒绝；产物缺失记未决而非失败；
- R01：按配置构造 Target 客户端；未支持类型直接报错，不静默退回 mock；健康检查做真实探测。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .contracts import AnswerCandidate, EpisodeOutcome, Task, new_id, stable_hash
from .datasets import load_split, write_json
from .judge import EpisodeEvaluation, JudgeEval, Rubric, compute_metrics, decide_outcome
from .methods.independent import EpisodeArtifacts, run_episode


# --------------------------------------------------------------------------
# 配置与路径
# --------------------------------------------------------------------------
def load_config(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


def results_root(cfg: Dict[str, Any]) -> str:
    exp = cfg.get("experiment", {}) or {}
    return os.path.join(exp.get("results_dir", "results"), exp.get("id", "exp"))


def _episode_id(task_id: str, seed: int, method: str) -> str:
    h = hashlib.sha256(f"{task_id}|{seed}|{method}".encode("utf-8")).hexdigest()[:10]
    return f"ep_{h}"


# --------------------------------------------------------------------------
# 计划：先冻结 episode 清单与每题 rubric
# --------------------------------------------------------------------------
def resolve_rubric(cfg: Dict[str, Any], task: Task) -> Rubric:
    """判据冻结：显式 rubric_elements 优先；否则用任务要求要素（accept=要素名）。"""
    judge_cfg = (cfg.get("method", {}) or {}).get("judge", {}) or {}
    elements = judge_cfg.get("rubric_elements")
    if not elements:
        elements = [{"key": e, "accept": [], "critical": True} for e in (task.runtime_hints.get("elements") or [])]
    return Rubric(
        rubric_id=judge_cfg.get("rubric_id", "inline"),
        elements=[dict(e) for e in elements],
        completion_threshold=float(judge_cfg.get("completion_threshold", 0.6)),
        full_threshold=float(judge_cfg.get("full_threshold", 0.8)),
        version=str(judge_cfg.get("version", "v1")),
    )


def build_plan(cfg: Dict[str, Any], *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    path = ds.get("path", "fixtures/synthetic_library/tasks.jsonl")
    exp_seed = int((cfg.get("experiment", {}) or {}).get("seed", 20261110))
    split_seed = int(ds.get("split_seed", exp_seed))
    result, dev, held, manifest = load_split(name, path, seed=split_seed, dev_ratio=int(ds.get("dev_ratio", 2)))
    pool = dev if ds.get("split", "dev") == "dev" else held
    n = limit if limit is not None else ds.get("limit")
    tasks = pool[offset : offset + n] if n is not None else pool[offset:]
    method = (cfg.get("method", {}) or {}).get("name", "independent")
    entries = []
    for t in tasks:
        rubric = resolve_rubric(cfg, t)
        entries.append(
            {
                "episode_id": _episode_id(t.task_id, exp_seed, method),
                "task_id": t.task_id,
                "task": {
                    "task_id": t.task_id,
                    "prompt": t.prompt,
                    "primary_dataset": t.primary_dataset,
                    "necessary_context": t.necessary_context,
                    "category": t.category,
                    "runtime_hints": t.runtime_hints,
                },
                "rubric": rubric.to_dict(),
                "seed": exp_seed,
                "duplicate_group_id": getattr(t, "duplicate_group_id", "") or t.task_id,
            }
        )
    return {
        "experiment_id": (cfg.get("experiment", {}) or {}).get("id"),
        "config_hash": stable_hash(cfg),
        "dataset": name,
        "split": ds.get("split", "dev"),
        "dataset_manifest": manifest.to_dict(),
        "split_seed": split_seed,
        "episode_seed": exp_seed,
        "episodes": entries,
        "n_episodes": len(entries),
    }


def write_plan(cfg: Dict[str, Any], root: str, *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    if os.path.exists(os.path.join(root, "plan.json")):
        raise FileExistsError(f"实验已存在：{root}（同 ID 重跑会污染分母，拒绝执行）")
    plan = build_plan(cfg, limit=limit, offset=offset)
    os.makedirs(root, exist_ok=True)
    write_json(plan, os.path.join(root, "plan.json"))
    write_json({e["task_id"]: e["rubric"] for e in plan["episodes"]}, os.path.join(root, "rubrics.json"))
    return plan


def load_plan(cfg: Dict[str, Any]) -> Dict[str, Any]:
    root = results_root(cfg)
    path = os.path.join(root, "plan.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少计划文件：{path}（先运行 run 或 prepare-plan）")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# 数据准备与检查
# --------------------------------------------------------------------------
def prepare_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan = build_plan(cfg)
    ds = cfg.get("datasets", {}) or {}
    out_dir = ds.get("manifests_dir", "data/manifests")
    name = ds.get("adapter", "synthetic_library")
    write_json(plan["dataset_manifest"], os.path.join(out_dir, f"{name}.dataset_manifest.json"))
    write_json(
        {
            "split_seed": plan["episodes"][0]["seed"] if plan["episodes"] else None,
            "tasks": [e["task_id"] for e in plan["episodes"]],
            "episode_ids": [e["episode_id"] for e in plan["episodes"]],
        },
        os.path.join(out_dir, f"{name}.split_manifest.json"),
    )
    return {
        "adapter": name,
        "loaded": plan["dataset_manifest"]["included"],
        "planned": plan["n_episodes"],
        "excluded": len(plan["dataset_manifest"]["excluded"]),
        "raw_sha256": plan["dataset_manifest"]["raw_sha256"],
        "groups": len(plan["dataset_manifest"]["duplicate_groups"]),
    }


def check_data(cfg: Dict[str, Any]) -> Dict[str, Any]:
    plan = build_plan(cfg)
    ds = cfg.get("datasets", {}) or {}
    name = ds.get("adapter", "synthetic_library")
    manifest = plan["dataset_manifest"]
    groups = manifest["duplicate_groups"]
    problems: List[str] = []
    for gid, ids in groups.items():
        splits = {}
        for tid in ids:
            splits.setdefault(tid, gid)
        if len(ids) > 1 and len(set(splits.values())) > 1:  # pragma: no cover - 组内单值
            problems.append(f"重复组 {gid} 跨 split")
    if not plan["episodes"]:
        problems.append("计划为空：开发池不足")
    if any(not e["task"]["prompt"] for e in plan["episodes"]):
        problems.append("存在空 prompt")
    return {
        "adapter": name,
        "loaded": manifest["included"],
        "planned": plan["n_episodes"],
        "groups": len(groups),
        "excluded": manifest["excluded"],
        "ok": not problems,
        "problems": problems,
        "raw_sha256": manifest["raw_sha256"],
    }


# --------------------------------------------------------------------------
# 服务检查（真实探测；探针成本独立）
# --------------------------------------------------------------------------
def check_services(cfg: Dict[str, Any]) -> Dict[str, Any]:
    from .clients import probe_openai

    target = cfg.get("target", {}) or {}
    kind = (target.get("type") or "mock").lower()
    if kind == "mock":
        return {"target": {"type": "mock", "ok": True, "probed": False, "note": "显式配置 mock；不产生真实调用"}}
    if kind == "openai":
        probe = probe_openai(
            target.get("base_url", ""),
            target.get("api_key_env", "OPENAI_API_KEY"),
            float(target.get("timeout_s", 10)),
        )
        probe.update({"type": "openai", "model": target.get("model", ""), "probed": True})
        return {"target": probe}
    return {"target": {"type": kind, "ok": False, "probed": False, "error": "unsupported target type"}}


# --------------------------------------------------------------------------
# 运行
# --------------------------------------------------------------------------
def run_experiment(cfg: Dict[str, Any], *, limit: Optional[int] = None, offset: int = 0) -> Dict[str, Any]:
    root = results_root(cfg)
    plan = write_plan(cfg, root, limit=limit, offset=offset)
    write_json(
        {
            "experiment_id": plan["experiment_id"],
            "config_hash": plan["config_hash"],
            "n_episodes": plan["n_episodes"],
            "dataset": plan["dataset"],
            "split": plan["split"],
        },
        os.path.join(root, "manifest.json"),
    )
    with open(os.path.join(root, "config.resolved.yaml"), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=True)
    write_json(plan["dataset_manifest"], os.path.join(root, "dataset_manifest.json"))
    write_json(check_services(cfg), os.path.join(root, "preflight.json"))
    write_json(cfg.get("deployment", {}) or {"note": "部署由研究者提供；此处只记录可得信息"}, os.path.join(root, "deployment_manifest.json"))

    artifacts: List[EpisodeArtifacts] = []
    for entry in plan["episodes"]:
        task = Task(**entry["task"])
        rubric = Rubric.from_frozen(entry["rubric"])
        art = run_episode(task, config=cfg, results_root=root, episode_id=entry["episode_id"], rubric=rubric)
        ep_dir = os.path.join(root, "episodes", art.episode_id)
        os.makedirs(ep_dir, exist_ok=True)
        _write_candidates(art, os.path.join(ep_dir, "candidates.jsonl"))
        write_final(art, os.path.join(ep_dir, "final.json"))
        artifacts.append(art)

    ledgers = [a.store.ledger() for a in artifacts]
    summary = {
        "experiment_id": plan["experiment_id"],
        "episodes": len(artifacts),
        "target_calls": [l["target_calls"] for l in ledgers],
        "ledger_total": {
            "target_calls": sum(l["target_calls"] for l in ledgers),
            "served_calls": sum(l["served_calls"] for l in ledgers),
            "cache_hits": sum(l["cache_hits"] for l in ledgers),
            "tokens_in": sum(l["tokens_in"] for l in ledgers),
            "tokens_out": sum(l["tokens_out"] for l in ledgers),
            "tokens_in_unknown_calls": sum(l["tokens_in_unknown_calls"] for l in ledgers),
        },
    }
    write_json(summary, os.path.join(root, "ledger.json"))
    return summary


def _write_candidates(art: EpisodeArtifacts, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for c in art.candidates():
            f.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")


def write_final(art: EpisodeArtifacts, path: str, outcome: Optional[str] = None, verdict_id: Optional[str] = None, basis: str = "") -> Dict[str, Any]:
    payload = {
        "episode_id": art.episode_id,
        "task_id": art.task.task_id,
        "outcome": outcome,
        "verdict_id": verdict_id,
        "termination_reason": art.termination_reason,
        "final_candidate_id": art.final_candidate.candidate_id if art.final_candidate else None,
        "steps": art.steps,
        "ledger": art.store.ledger(),
        "basis": basis,
    }
    write_json(payload, path)
    return payload


REQUIRED_FINAL_FIELDS = ("episode_id", "task_id", "final_candidate_id", "termination_reason")


def _has_authoritative_terminate(ep_dir: str) -> bool:
    path = os.path.join(ep_dir, "events.jsonl")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                if json.loads(line).get("type") == "terminate":
                    return True
            except json.JSONDecodeError:
                continue
    return False


# --------------------------------------------------------------------------
# 评价（按计划对账，不重跑 Target）
# --------------------------------------------------------------------------
def load_plan_from_root(root: str) -> Dict[str, Any]:
    path = os.path.join(root, "plan.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"缺少计划文件：{path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def evaluate_experiment(cfg: Dict[str, Any], root: Optional[str] = None) -> Dict[str, Any]:
    root = root or results_root(cfg)
    # S08：评价必须使用被评价目录自己的冻结计划；config 只用于定位默认 root
    plan = load_plan_from_root(root)
    eval_run_id = new_id("eval")
    eval_dir = os.path.join(root, "evaluations", eval_run_id)
    os.makedirs(eval_dir, exist_ok=True)
    write_json({"rubrics": "plan.json#/episodes", "judge_ref": "rule@v1"}, os.path.join(eval_dir, "config.json"))

    evaluations: List[EpisodeEvaluation] = []
    records: List[Dict[str, Any]] = []
    for entry in plan["episodes"]:
        ep_id = entry["episode_id"]
        task_id = entry["task_id"]
        task = Task(**entry["task"])
        rubric = Rubric.from_frozen(entry["rubric"])
        judge = JudgeEval(rubric)
        ep_dir = os.path.join(root, "episodes", ep_id)
        final_path = os.path.join(ep_dir, "final.json")

        verdict = None
        candidate = None
        failure_established = False
        basis = ""

        if not os.path.exists(final_path):
            basis = "missing_final:artifact_unavailable"
            outcome = EpisodeOutcome.UNRESOLVED
        else:
            try:
                final = json.load(open(final_path, "r", encoding="utf-8"))
            except json.JSONDecodeError:
                final = None
            missing = [k for k in REQUIRED_FINAL_FIELDS if not isinstance(final, dict) or k not in final]
            if missing:
                # 字段缺失 ≠ 已证实的"正常结束无答案"：记 U，不写进 F
                basis = "incomplete_final:missing_" + ",".join(missing or ["json"])
                outcome = EpisodeOutcome.UNRESOLVED
                final = None
            else:
                cand_id = final.get("final_candidate_id")
                if cand_id:
                    candidate = _load_final_candidate(ep_dir, cand_id)
                    if candidate is None:
                        basis = "missing_candidate:artifact_unavailable"
                        outcome = EpisodeOutcome.UNRESOLVED
                    else:
                        verdict = judge.evaluate(task, candidate, eval_run_id)
                        ev = decide_outcome(
                            episode_id=ep_id, task_id=task_id, final_answer=candidate, verdict=verdict, failure_established=False
                        )
                        outcome, basis = ev.outcome, ev.reason
                elif _has_authoritative_terminate(ep_dir):
                    # 只有权威 terminate 事件确认"正常结束且无答案"才计失败
                    failure_established = True
                    ev = decide_outcome(
                        episode_id=ep_id, task_id=task_id, final_answer=None, verdict=None, failure_established=True
                    )
                    outcome, basis = ev.outcome, ev.reason
                else:
                    outcome, basis = EpisodeOutcome.UNRESOLVED, "no_answer_without_terminate_event"

        evaluation = EpisodeEvaluation(
            ep_id, task_id, outcome, verdict, basis, group_id=entry.get("duplicate_group_id", "") or task_id
        )
        evaluations.append(evaluation)
        records.append(
            {
                "episode_id": ep_id,
                "task_id": task_id,
                "outcome": outcome.value,
                "basis": basis,
                "verdict_id": verdict.verdict_id if verdict else None,
                "class": verdict.cls if verdict else None,
            }
        )
        if verdict is not None:
            with open(os.path.join(eval_dir, "verdicts.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(verdict), ensure_ascii=False) + "\n")
        if os.path.exists(final_path):
            final = json.load(open(final_path, "r", encoding="utf-8"))
            final["outcome"] = outcome.value
            final["verdict_id"] = verdict.verdict_id if verdict else None
            final["basis"] = basis
            write_json(final, final_path)

    metrics = compute_metrics(evaluations)
    write_json(metrics, os.path.join(eval_dir, "metrics.json"))
    with open(os.path.join(eval_dir, "records.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"eval_run_id": eval_run_id, "dir": eval_dir, **metrics}


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
    records = []
    for line in open(os.path.join(ep_dir, "events.jsonl"), "r", encoding="utf-8"):
        ev = json.loads(line)
        if ev["type"] == "state_write":
            records.append({"seq": ev["seq"], "node": ev.get("node_id"), "new_facts": len(ev["payload"].get("new_facts", []))})
    return {"episode": os.path.basename(ep_dir.rstrip("/")), "nodes": records, "note": "节点级干预需要重跑装配，属 analysis 成本"}
