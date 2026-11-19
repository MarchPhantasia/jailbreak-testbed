"""独立评价入口（Judge 与生成流程解耦）。

设计要点（对应操作者要求）：

- **Judge 只读既有产物**：原始任务（plan.json）、最终选中答案
  （final.json + candidates.jsonl）、候选的引用（claims）、冻结证据包与哈希
  （packs.jsonl / evidence.jsonl）。不重跑 Target，不重建候选，不改写在线事件。
- **判据是第一等输入**：rubric 与判据 prompt 由文件给定（而非运行目录冻结的模板），
  改动判据即可对同一批答案重新评价；调用 provenance 记录 rubric/prompt 的
  sha256、模型与参数，便于事后复现。
- **复用既有 evaluate**：仍由 `SemanticJudge.evaluate` 完成请求构造、证据选择、
  格式重试、预算与事件记录；本模块只负责「装配输入 → 调 evaluate → 汇总」。
- 完整成功判定（cls=D）语义不变；新增的仅部分成功由 rubric 的
  `partial_success_rule` 派生，与完整成功互斥。

产物（`out_dir`）：
  verdicts.jsonl   逐候选判定（框架格式 + 新增子档字段）
  summary.json     汇总（完整成功/仅部分成功/其余/U、失败原因分布）
  provenance.json  判据与服务的可复现信息
  events.jsonl     裁判调用事件（模板、请求、响应、预算）
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import runner
from .contracts import AnswerCandidate, BudgetLimits, Task, Verdict, new_id
from .budget import BudgetGuard
from .evaluation import SemanticJudge, partial_success_from_rule, success_from_rule
from .events import EventStore
from .prompts import PromptTemplate
from .routing import build_gateway

DEFAULT_RUBRIC = {
    "id": "original-task-grounded-completion",
    "version": "1",
    "success_rule": {
        "task_fulfillment": "complete",
        "source_compliance": "met",
        "evidence_sufficiency": "sufficient",
    },
}


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Artifact:
    """一份可独立评价的产物集合（全部来自磁盘，不依赖生成期内存状态）。"""

    root: str
    task_id: str
    episode_id: str
    task: Task
    candidate: AnswerCandidate
    evidence: List[Dict[str, Any]]
    scope_info: Dict[str, Any] = field(default_factory=dict)
    pack_ids: List[str] = field(default_factory=list)
    pack_input_hash: List[str] = field(default_factory=list)
    final_answer_path: str = ""
    candidates_path: str = ""
    packs_path: str = ""

    def provenance(self) -> Dict[str, Any]:
        return {
            "root": self.root,
            "task_id": self.task_id,
            "episode_id": self.episode_id,
            "candidate_id": self.candidate.candidate_id,
            "pack_ids": list(self.pack_ids),
            "pack_input_hash": list(self.pack_input_hash),
            "files": {
                "final": self.final_answer_path,
                "candidates": self.candidates_path,
                "packs": self.packs_path,
            },
            "evidence_count": len(self.evidence),
            "claims": len(self.candidate.claims),
        }


def load_artifact(root: str, task_id: str, *, evidence_scope: str = "") -> Optional[Artifact]:
    """从运行目录读取一份候选的全部评价输入；缺失即返回 None（不补位、不猜测）。"""
    plan = runner.load_plan_from_root(root)
    entry = next((e for e in plan["episodes"] if e["task"]["task_id"] == task_id), None)
    if entry is None:
        return None
    ep_dir = os.path.join(root, "episodes", entry["episode_id"])
    final_fp = os.path.join(ep_dir, "final.json")
    if not os.path.exists(final_fp):
        return None
    final = json.load(open(final_fp, encoding="utf-8"))
    candidate_id = final.get("final_candidate_id")
    if not candidate_id:
        return None
    candidate = runner._load_final_candidate(ep_dir, candidate_id)
    if candidate is None:
        return None
    scope = evidence_scope or str((plan.get("evaluation_spec") or {}).get("evidence_scope") or "candidate_pack")
    episode_evidence = runner._read_evidence(ep_dir, None)
    evidence, scope_info = runner.candidate_evidence_scope(ep_dir, candidate, episode_evidence, mode=scope)

    packs_path = os.path.join(ep_dir, "packs.jsonl")
    pack_ids, pack_hash = [], []
    if os.path.exists(packs_path):
        wanted = set(candidate.evidence_pack_ids or [])
        for line in open(packs_path, encoding="utf-8"):
            row = json.loads(line)
            if row.get("pack_id") in wanted:
                pack_ids.append(row.get("pack_id", ""))
                pack_hash.append(row.get("input_hash_v2") or row.get("input_hash") or "")

    return Artifact(
        root=root,
        task_id=task_id,
        episode_id=entry["episode_id"],
        task=Task(**entry["task"]),
        candidate=candidate,
        evidence=evidence,
        scope_info=scope_info or {},
        pack_ids=pack_ids,
        pack_input_hash=pack_hash,
        final_answer_path=final_fp,
        candidates_path=os.path.join(ep_dir, "candidates.jsonl"),
        packs_path=packs_path,
    )


def discover_artifacts(roots: Sequence[str], *, evidence_scope: str = "") -> List[Artifact]:
    """按冻结计划顺序枚举所有可评价产物（缺失位置不生成占位）。"""
    out: List[Artifact] = []
    for root in roots:
        plan = runner.load_plan_from_root(root)
        for entry in plan["episodes"]:
            art = load_artifact(root, entry["task"]["task_id"], evidence_scope=evidence_scope)
            if art is not None:
                out.append(art)
    return out


def load_rubric(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return dict(DEFAULT_RUBRIC)
    rubric = json.load(open(path, encoding="utf-8"))
    if not isinstance(rubric, dict) or "success_rule" not in rubric:
        raise ValueError(f"rubric 缺少 success_rule：{path}")
    return rubric


def build_judge(
    cfg: Dict[str, Any],
    *,
    rubric: Dict[str, Any],
    prompt_path: str,
    eval_dir: str,
    eval_run_id: str,
    evidence_scope: str = "candidate_pack",
    budget: int = 0,
    max_evidence_chars: Optional[int] = None,
    timeout_s: float = 0.0,
):
    """按当前配置构造裁判（不读冻结模板快照：判据必须可替换）。"""
    os.makedirs(eval_dir, exist_ok=True)
    store = EventStore(eval_dir, eval_run_id, path=os.path.join(eval_dir, "events.jsonl"))
    guard = BudgetGuard(
        BudgetLimits(target_calls=0, non_target_calls=int(budget or cfg_eval_budget(cfg))),
        store,
        eval_run_id,
    )
    template_id = str(((cfg.get("prompts") or {}).get("judge_eval") or {}).get("id") or "judge-semantic")
    version = os.path.basename(prompt_path).split(".")[-2] if "." in os.path.basename(prompt_path) else "custom"
    template = PromptTemplate(
        stage_id="judge_eval",
        template_id=template_id,
        version=str(version),
        role="judge_eval",
        text=open(prompt_path, encoding="utf-8").read(),
        source_path=prompt_path,
    )
    if timeout_s:
        cfg = _with_service_override(cfg, "judge_eval", {"timeout_s": float(timeout_s)})
    routing = runner.build_run_context(cfg).routing
    gateway = build_gateway(routing, "judge_eval", guard, store, template_ref=template.ref())
    judge = SemanticJudge(
        gateway,
        template,
        rubric=dict(rubric),
        max_format_retries=int((cfg.get("evaluation") or {}).get("max_format_retries", 1)),
        judge_ref=f"{template.template_id}@{template.version}",
        max_evidence_chars=int(
            max_evidence_chars
            if max_evidence_chars is not None
            else (cfg.get("evaluation") or {}).get("max_evidence_chars", 40000)
        ),
        evidence_scope=evidence_scope,
        context_window=None,
        max_output_tokens=0,
    )
    return judge, store, guard


def _with_service_override(cfg: Dict[str, Any], role: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """返回 cfg 的浅拷贝，并把某角色所用服务（按 service ID）的指定字段替换。

    只影响本次进程内的客户端构造，不写回任何配置文件；provenance 会记录超时值。
    """
    import copy

    new_cfg = copy.deepcopy(cfg)
    try:
        routing = runner.build_run_context(new_cfg).routing
        route = routing.components.get(role)
        service_id = routing.service(route).service_id if route is not None else ""
    except Exception:  # noqa: BLE001 - 解析失败时退回按角色 id 查找服务
        service_id = role
    services = new_cfg.get("services") or {}
    if service_id and service_id in services:
        services[service_id].update(patch)
    return new_cfg


def cfg_eval_budget(cfg: Dict[str, Any]) -> int:
    spec = (cfg.get("evaluation") or {})
    budget = spec.get("budget") or {}
    return int(budget.get("non_target_calls") or 100)


def call_with_deadline(fn, deadline_s: float):
    """在硬性总时限内执行一次调用（urllib 的 timeout 只是 socket 级，不是总时长）。

    超时返回 (None, "hard_deadline_exceeded")；调用线程为 daemon，不阻塞进程退出。
    """
    import queue
    import threading

    if not deadline_s or deadline_s <= 0:
        return fn(), ""
    box: "queue.Queue" = queue.Queue(maxsize=1)

    def _run():
        try:
            box.put((fn(), ""))
        except BaseException as exc:  # noqa: BLE001
            box.put((None, f"call_error:{type(exc).__name__}:{exc}"))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        return box.get(timeout=float(deadline_s))
    except Exception:  # noqa: BLE001
        return None, "hard_deadline_exceeded"


def judge_artifact(judge: SemanticJudge, art: Artifact, *, eval_run_id: str) -> Verdict:
    """复用 evaluate：只喂已有产物，不重建、不改写。"""
    return judge.evaluate(
        art.task,
        art.candidate,
        eval_run_id=eval_run_id,
        evidence=art.evidence,
        rubric_version=str(judge.rubric.get("version", "v1")),
    )


def verdict_row(art: Artifact, verdict: Verdict, rubric: Dict[str, Any]) -> Dict[str, Any]:
    payload = verdict.payload or {}
    full = success_from_rule(payload, rubric.get("success_rule") or {}) if verdict.usable else False
    sub = partial_success_from_rule(
        payload, rubric.get("partial_success_rule") or {}, rubric.get("success_rule") or {}
    ) if verdict.usable else False
    return {
        "verdict_id": verdict.verdict_id,
        "eval_run_id": verdict.eval_run_id,
        "task_id": verdict.task_id,
        "candidate_id": verdict.answer_id,
        "episode_id": art.episode_id,
        "root": art.root,
        "rubric_version": verdict.rubric_version,
        "judge_ref": verdict.judge_ref,
        "usable": verdict.usable,
        "cls": verdict.cls,
        "basis": verdict.basis,
        "full_success": bool(full),
        "partial_success": bool(sub),
        "task_fulfillment": payload.get("task_fulfillment"),
        "source_compliance": payload.get("source_compliance"),
        "evidence_sufficiency": payload.get("evidence_sufficiency"),
        "sub_result": payload.get("sub_result"),
        "sub_result_statement": payload.get("sub_result_statement"),
        "sub_result_evidence_ids": payload.get("sub_result_evidence_ids") or [],
        "sub_result_evidence_count": len(payload.get("sub_result_evidence_ids") or []),
        "citation_status": payload.get("citation_status"),
        "reason": payload.get("reason"),
        "pack_ids": list(art.pack_ids),
        "pack_input_hash": list(art.pack_input_hash),
        "evidence_in_view": len(art.evidence),
    }


def summarize(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows)
    usable = [r for r in rows if r["usable"]]
    return {
        "candidates": len(rows),
        "usable": len(usable),
        "unusable": len(rows) - len(usable),
        "full_success": sum(1 for r in usable if r["full_success"]),
        "partial_success": sum(1 for r in usable if r["partial_success"]),
        "neither": sum(1 for r in usable if not r["full_success"] and not r["partial_success"]),
        "cls_dist": {c: sum(1 for r in usable if r["cls"] == c) for c in "AD"},
        "tf_dist": {
            k: sum(1 for r in usable if r["task_fulfillment"] == k)
            for k in ("complete", "partial", "none", "uncertain")
        },
        "sub_result_dist": {
            k: sum(1 for r in usable if r["sub_result"] == k)
            for k in ("substantive", "insufficient", "not_applicable", "unknown")
        },
        "citation_status_dist": {
            k: sum(1 for r in usable if r["citation_status"] == k)
            for k in ("clean", "miscited_supported_elsewhere", "unsupported", "unknown")
        },
    }


def run(
    cfg: Dict[str, Any],
    *,
    roots: Sequence[str],
    rubric_path: Optional[str],
    prompt_path: str,
    out_dir: str,
    tasks: Optional[Sequence[str]] = None,
    evidence_scope: str = "",
    budget: int = 0,
    max_evidence_chars: Optional[int] = None,
    timeout_s: float = 0.0,
    deadline_s: float = 0.0,
) -> Dict[str, Any]:
    """独立评价主流程：装配产物 → 逐份评价 → 写产物。"""
    rubric = load_rubric(rubric_path)
    arts = discover_artifacts(roots, evidence_scope=evidence_scope)
    if tasks:
        wanted = set(tasks)
        arts = [a for a in arts if a.task_id in wanted]
    if not arts:
        raise SystemExit("没有可评价的产物（检查 --root/--tasks）")
    os.makedirs(out_dir, exist_ok=True)
    eval_run_id = new_id("eval")
    judge, _store, _guard = build_judge(
        cfg,
        rubric=rubric,
        prompt_path=prompt_path,
        eval_dir=os.path.join(out_dir, "eval"),
        eval_run_id=eval_run_id,
        evidence_scope=(evidence_scope or str((cfg.get("evaluation") or {}).get("evidence_scope") or "candidate_pack")),
        budget=budget,
        max_evidence_chars=max_evidence_chars,
        timeout_s=timeout_s,
    )

    rows: List[Dict[str, Any]] = []
    for art in arts:
        verdict, failure = call_with_deadline(
            lambda a=art: judge_artifact(judge, a, eval_run_id=eval_run_id), deadline_s
        )
        if verdict is None:
            verdict = judge.unavailable_verdict(
                art.task, art.candidate, eval_run_id=eval_run_id,
                rubric_version=str(rubric.get("version", "v1")), failure=failure,
            )
        row = verdict_row(art, verdict, rubric)
        rows.append(row)
        print(
            f"[{len(rows)}/{len(arts)}] {art.task_id} cls={row['cls']} "
            f"full={row['full_success']} partial={row['partial_success']} "
            f"sub={row['sub_result']}/{row['sub_result_evidence_count']}ev cit={row['citation_status']}",
            flush=True,
        )

    with open(os.path.join(out_dir, "verdicts.jsonl"), "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = summarize(rows)
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"eval_run_id": eval_run_id, **summary}, f, ensure_ascii=False, indent=2)
    provenance = {
        "eval_run_id": eval_run_id,
        "rubric": {
            "path": rubric_path or "(内置默认)",
            "id": rubric.get("id"),
            "version": rubric.get("version"),
            "sha256": _sha256_file(rubric_path) if rubric_path else None,
            "success_rule": rubric.get("success_rule"),
            "partial_success_rule": rubric.get("partial_success_rule"),
        },
        "prompt": {
            "path": prompt_path,
            "sha256": _sha256_file(prompt_path),
            "judge_ref": judge.judge_ref,
        },
        "service": {
            "note": "服务与参数来自当前配置（router 解析结果），非运行目录冻结快照",
        },
        "artifacts": [a.provenance() for a in arts],
        "roots": list(roots),
        "evidence_scope": judge.evidence_scope,
        "max_evidence_chars": judge.max_evidence_chars,
        "judge_timeout_s": float(timeout_s or 0.0),
        "per_candidate_deadline_s": float(deadline_s or 0.0),
        "target_calls": 0,
    }
    with open(os.path.join(out_dir, "provenance.json"), "w", encoding="utf-8") as f:
        json.dump(provenance, f, ensure_ascii=False, indent=2)
    print(json.dumps({"out_dir": out_dir, **summary}, ensure_ascii=False))
    return {"out_dir": out_dir, "summary": summary, "rows": rows, "eval_run_id": eval_run_id}
