"""独立方法的 episode 执行（手册 §12.1）。

只做编排：装配组件 → 循环 propose/execute → 冻结候选。
结果判定（success/failure/unresolved）不在这里落定，交给 EvaluationRun。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..assembly import Assembler, make_selector
from ..budget import BudgetExhausted, BudgetGuard
from ..clients import MockGateway, ModelGateway, OpenAICompatGateway
from ..contracts import Action, ActionType, AnswerCandidate, BudgetLimits, Episode, SessionMode, Status, Task, new_id, stable_hash
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


def build_target_gateway(cfg: Dict[str, Any], task: Task, guard: BudgetGuard, store: EventStore) -> ModelGateway:
    """按配置构造 Target 客户端；未支持类型直接报错，不静默退回 mock（R01）。"""
    target = cfg.get("target", {}) or {}
    kind = (target.get("type") or "mock").lower()
    if kind == "mock":
        scripts = (task.runtime_hints or {}).get("target_scripts") or {"*": [{"status": "OK", "text": "（模拟 Target 无内容）"}]}
        return MockGateway(guard, store, scripts=_to_script_items(scripts), model_name=target.get("model", "mock"))
    if kind in ("openai", "openai_compatible", "http"):
        if not target.get("base_url"):
            raise ValueError("target.type=openai 需要 base_url")
        return OpenAICompatGateway(
            guard,
            store,
            base_url=target["base_url"],
            model=target.get("model", "target"),
            api_key_env=target.get("api_key_env", "OPENAI_API_KEY"),
            timeout=float(target.get("timeout_s", 60)),
        )
    raise ValueError(f"未支持的 target.type：{kind}（不做隐式 mock 回退）")


def build_local_gateway(cfg: Dict[str, Any], guard: BudgetGuard, store: EventStore) -> ModelGateway:
    local = cfg.get("attacker", {}) or {}
    kind = (local.get("type") or "mock").lower()
    if kind == "mock":
        return MockGateway(guard, store, scripts={"*": [{"status": "OK", "text": "{}"}]}, model_name=local.get("model", "mock-local"))
    if kind in ("openai", "openai_compatible", "http"):
        return OpenAICompatGateway(
            guard, store, local["base_url"], local.get("model", "attacker"), local.get("api_key_env", "OPENAI_API_KEY")
        )
    raise ValueError(f"未支持的 attacker.type：{kind}")


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
    rubric: Optional[Rubric] = None,
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
        target_gateway = build_target_gateway(config, task, guard, store)
    if local_gateway is None:
        local_gateway = build_local_gateway(config, guard, store)

    if rubric is None:
        from ..runner import resolve_rubric

        rubric = resolve_rubric(config, task)
    elements = rubric.elements or [{"key": e, "accept": [], "critical": True} for e in (task.runtime_hints or {}).get("elements", [])]
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
        branch_width=int(search_cfg.get("branch_width", 1)),
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
    last_action: Optional[Action] = None
    pending_retry = False

    def _assemble_now(reason: str) -> None:
        nonlocal steps
        node_id = engine.current_node_id
        act = Action(
            action_id=new_id("act"),
            type=ActionType.ASSEMBLE,
            params={"k": int(assembly_cfg.get("k", 1))},
            based_on_state_seq=engine.states[node_id].source_event_seq,
            node_id=node_id,
            view_id=engine.nodes[node_id].view_ref,
            rationale=reason,
        )
        store.append("action_proposed", role="engine", node_id=node_id, payload={"action_id": act.action_id, "type": act.type.value, "rationale": reason})
        engine.execute(act)
        steps += 1

    while not engine.terminated and steps < max_steps:
        node_id = engine.select_node() or engine.current_node_id
        state = engine.states[node_id]

        if pending_retry and last_action is not None:
            action = last_action  # R08：重试原动作，不推进控制器序列
            pending_retry = False
        else:
            try:
                action = controller.propose(
                    task,
                    state,
                    elements=elements,
                    node_id=node_id,
                    view_id=engine.nodes[node_id].view_ref,
                    budget=guard.snapshot(),
                    can_assemble=True,
                )
            except BudgetExhausted:
                # S07：本地额度耗尽属于常规停止，必须记账收尾而非抛出
                store.append("action_rejected", role="controller", node_id=node_id,
                             payload={"reason": "local_budget_exhausted"})
                engine.terminate("local_budget_exhausted")
                break

        if action is None:
            # S07：保留组件真实状态（拒答 ≠ 格式错误）
            status = getattr(controller, "last_status", Status.INVALID_OUTPUT)
            decision = recovery.handle("controller", status, attempt_key="controller")
            steps += 1
            if decision.action in ("retry_same", "retry_rephrase"):
                continue
            if decision.action == "fallback_assemble":
                _assemble_now(decision.reason)
                break
            engine.terminate(decision.reason)
            break

        store.append(
            "action_proposed",
            role="controller",
            node_id=node_id,
            payload={"action_id": action.action_id, "type": action.type.value, "params": action.params, "rationale": action.rationale},
        )
        result = engine.execute(action)
        steps += 1
        last_action = action

        if result.status is Status.BUDGET_EXHAUSTED:
            break  # R09：交给收尾逻辑，用剩余本地额度装配

        if result.retryable and not result.rejected:
            decision = recovery.handle("target", result.status, attempt_key="target")
            if decision.action in ("retry_same", "retry_rephrase") and guard.can_spend_target():
                pending_retry = True
                continue
            if decision.action == "fallback_assemble":
                _assemble_now(decision.reason)
                break
            if decision.action == "stop":
                engine.terminate(decision.reason)
                break

        if engine.final_candidate is not None and action.type is ActionType.ASSEMBLE and engine.stop_reason:
            engine.terminate("online_success")
            break

    if not engine.terminated:
        if engine.final_candidate is None and guard.snapshot().get("non_target_calls_left", 1) != 0:
            _assemble_now("finalize")
        engine.terminate(engine.termination_reason or ("budget_exhausted" if engine.target_exhausted else "max_steps"))

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
