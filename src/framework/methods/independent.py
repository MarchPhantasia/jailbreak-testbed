"""独立方法的 episode 执行（手册 §12.1）。

只做编排：装配组件 → 循环 propose/execute → 冻结候选。
结果判定（success/failure/unresolved）不在这里落定，交给 EvaluationRun。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..assembly import Assembler, make_selector
from ..budget import BudgetGuard
from ..clients import MockGateway, ModelGateway
from ..contracts import ActionType, AnswerCandidate, Episode, BudgetLimits, SessionMode, Status, Task, stable_hash, new_id
from ..controller import Controller, ElementController, ScriptedController
from ..engine import Engine
from ..events import EventStore
from ..judge import JudgeOnline, Rubric
from ..recovery import RecoveryPolicy
from ..search import make_policy, make_scorer
from ..state import make_backend


@dataclass
class EpisodeArtifacts:
    episode_id: str
    task: Task
    store: EventStore
    engine: Engine
    controller_name: str
    final_candidate: Optional[AnswerCandidate]
    termination_reason: str
    steps: int = 0

    def candidates(self) -> List[AnswerCandidate]:
        return list(self.engine.candidates)


def build_controller(kind: str, *, queries: Optional[List[str]] = None, elements: Optional[List[str]] = None, gateway: Optional[ModelGateway] = None, renderer=None) -> Controller:
    if kind == "scripted":
        return ScriptedController(queries or [])
    if kind == "element":
        return ElementController(elements or [])
    if kind == "llm":
        from ..controller import LLMController

        if gateway is None or renderer is None:
            raise ValueError("llm controller 需要 gateway 与 renderer")
        return LLMController(gateway, renderer)
    raise ValueError(f"未知控制器：{kind}")


def run_episode(
    task: Task,
    *,
    config: Dict[str, Any],
    results_root: str,
    target_gateway: Optional[ModelGateway] = None,
    local_gateway: Optional[ModelGateway] = None,
    episode_id: Optional[str] = None,
) -> EpisodeArtifacts:
    """执行一条 episode。target_gateway 为空时按 config 构建 Mock。"""
    exp = config.get("experiment", {})
    budget_cfg = exp.get("budget", {}) or {}
    limits = BudgetLimits(
        target_calls=int(budget_cfg.get("target_calls", 20)),
        non_target_calls=int(budget_cfg.get("non_target_calls", 400)),
        tokens=budget_cfg.get("tokens"),
        wall_ms=budget_cfg.get("wall_ms"),
    )
    session_mode = SessionMode(exp.get("session_mode", "stateless"))
    method_cfg = config.get("method", {}) or {}
    state_cfg = method_cfg.get("state", {}) or {}
    assembly_cfg = method_cfg.get("assembly", {}) or {}
    search_cfg = method_cfg.get("search", {}) or {}
    judge_cfg = method_cfg.get("judge", {}) or {}
    recovery_cfg = method_cfg.get("recovery", {}) or {}

    eid = episode_id or new_id("ep")
    store = EventStore(results_root, eid)
    guard = BudgetGuard(limits, store, eid)

    if target_gateway is None:
        scripts = (task.runtime_hints or {}).get("target_scripts") or {"*": [{"status": "OK", "text": "（模拟 Target 无内容）"}]}
        target_gateway = MockGateway(guard, store, scripts=_to_script_items(scripts))
    if local_gateway is None:
        local_gateway = MockGateway(guard, store, scripts={"*": [{"status": "OK", "text": "{}"}]})

    elements = (
        (task.runtime_hints or {}).get("elements")
        or method_cfg.get("elements")
        or [e["key"] for e in (judge_cfg.get("rubric_elements") or [])]
    )
    rubric = Rubric(
        rubric_id=judge_cfg.get("rubric_id", "inline"),
        elements=judge_cfg.get("rubric_elements") or [{"key": e, "critical": True} for e in elements],
        completion_threshold=float(judge_cfg.get("completion_threshold", 0.6)),
        full_threshold=float(judge_cfg.get("full_threshold", 0.8)),
        version=str(judge_cfg.get("version", "v1")),
    )
    backend = make_backend(state_cfg.get("backend", "fox"), elements, max_chars=int(state_cfg.get("max_chars", 400)))
    engine = Engine(
        task=task,
        episode=Episode(
            episode_id=eid,
            task_id=task.task_id,
            method_version=method_cfg.get("name", "independent@v0.1"),
            config_hash=stable_hash(config),
            budget=limits,
            session_mode=session_mode,
            stage_policy_ref=config.get("stages", {}).get("ref", ""),
            recovery_policy_version=recovery_cfg.get("version", "r1"),
            seed=int(exp.get("seed", 0)),
        ),
        store=store,
        guard=guard,
        target_gateway=target_gateway,
        state_backend=backend,
        assembler=Assembler(),
        selector=make_selector(assembly_cfg.get("selector", "first")),
        judge_online=JudgeOnline(),
        rubric=rubric,
        search_policy=make_policy(search_cfg.get("policy", "linear")),
        scorer=make_scorer(search_cfg.get("scorer", "null")),
        elements=elements,
        k_candidates=int(assembly_cfg.get("k", 1)),
    )
    engine.start()

    controller = build_controller(
        method_cfg.get("controller", {}).get("type", "element"),
        queries=method_cfg.get("controller", {}).get("queries"),
        elements=elements,
        gateway=local_gateway,
        renderer=backend.render_for_controller,
    )
    recovery = RecoveryPolicy(store, **{k: v for k, v in recovery_cfg.items() if k in ("max_network_retries", "max_format_retries", "total_cap", "version")})

    max_steps = int(exp.get("max_steps", 12))
    steps = 0
    while not engine.terminated and steps < max_steps:
        node_id = engine.select_node() or engine.current_node_id
        state = engine.states[node_id]
        action = controller.propose(
            task,
            state,
            elements=elements,
            node_id=node_id,
            view_id=engine.nodes[node_id].view_ref,
            budget=guard.snapshot(),
            can_assemble=True,
        )
        if action is None:
            decision = recovery.handle("controller", Status.INVALID_OUTPUT, attempt_key="controller")
            if decision.action in ("retry_same", "retry_rephrase"):
                steps += 1
                continue
            action = controller._mk(ActionType.STOP, "", node_id, engine.nodes[node_id].view_ref, state.source_event_seq, decision.reason)
        store.append(
            "action_proposed",
            role="controller",
            node_id=node_id,
            payload={"action_id": action.action_id, "type": action.type.value, "params": action.params, "rationale": action.rationale},
        )
        result = engine.execute(action)
        steps += 1
        if result.status in (Status.TIMEOUT, Status.TRANSPORT_ERROR, Status.INVALID_OUTPUT) and not result.rejected:
            decision = recovery.handle("target", result.status, attempt_key="target")
            if decision.action in ("retry_same", "retry_rephrase") and guard.can_spend_target():
                continue
        if result.status is Status.BUDGET_EXHAUSTED:
            break
        if engine.final_candidate is not None and action.type is ActionType.ASSEMBLE:
            # 在线判定决定是否停止
            if engine.stop_reason:
                engine.terminate("online_success")
                break

    if not engine.terminated:
        engine.terminate("max_steps")

    return EpisodeArtifacts(
        episode_id=eid,
        task=task,
        store=store,
        engine=engine,
        controller_name=controller.name,
        final_candidate=engine.final_candidate,
        termination_reason=engine.termination_reason,
        steps=steps,
    )


def _to_script_items(scripts: Dict[str, Any]):
    """把 fixture 里的 {matcher: [{status,text}, ...]} 转成 MockGateway 的 CallResult 列表。"""
    from ..clients import CallResult

    out: Dict[str, Any] = {}
    for k, v in scripts.items():
        items = []
        for entry in v if isinstance(v, list) else [v]:
            if isinstance(entry, dict):
                status = Status(entry.get("status", "OK"))
                items.append(
                    CallResult(
                        call_id="",
                        status=status,
                        text=entry.get("text", ""),
                        finish_reason=entry.get("finish_reason", ""),
                        usage=entry.get("usage", {}) or {},
                    )
                )
            else:
                items.append(CallResult(call_id="", status=Status.OK, text=str(entry)))
        out[k] = items
    return out
