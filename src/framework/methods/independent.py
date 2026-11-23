"""独立方法的 episode 执行（手册 §12.1 / E01–E03）。

只做编排：装配组件 → 循环 propose/execute → 冻结候选。
结果判定（success/failure/unresolved）不在这里落定，交给 EvaluationRun。

R2 补充：
- 角色 → 服务 → 模型由 ``routing.RoutingPlan`` 决定，不再让所有本地角色共用同一个隐式网关；
- 请求生成写 ``request_generated`` 权威事件 + ``requests.jsonl`` 派生索引，Target 调用引用同一 request_id；
- 阶段额度耗尽时，若装配阶段与全局额度仍允许，用已有证据收尾而不是丢弃材料（N11）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..assembly import Assembler, make_assembler, make_selector
from ..budget import BudgetExhausted, BudgetGuard
from ..clients import MockGateway, ModelGateway, OpenAICompatGateway
from ..contracts import Action, ActionType, AnswerCandidate, BudgetLimits, Episode, SessionMode, Status, Task, new_id, stable_hash
from ..controller import Controller, ElementController, ScriptedController
from ..engine import DEFAULT_SYSTEM, Engine
from ..events import EventStore
from ..judge import JudgeOnline, Rubric
from ..prompts import PromptRegistry
from ..recovery import RecoveryPolicy
from ..requests import RequestLedger
from ..routing import RoutingPlan, build_gateway, resolve_routing
from ..search import make_policy, make_scorer
from ..state import make_backend
from . import wheel_release


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


def build_target_gateway(cfg: Dict[str, Any], task: Task, guard: BudgetGuard, store: EventStore, *, routing: Optional[RoutingPlan] = None) -> ModelGateway:
    """按配置构造 Target 客户端；未支持类型直接报错，不静默退回 mock（R01）。"""
    if routing is not None and (routing.new_registry or "target" not in cfg):
        gateway = build_gateway(routing, "target", guard, store)
        return gateway
    target = cfg.get("target", {}) or {}
    kind = (target.get("type") or "mock").lower()
    if kind == "mock":
        scripts = (task.runtime_hints or {}).get("target_scripts") or {"*": [{"status": "OK", "text": "（模拟 Target 无内容）"}]}
        gateway = MockGateway(guard, store, scripts=_to_script_items(scripts), model_name=target.get("model", "mock"))
        gateway.stage_id = "target_query"
        return gateway
    if kind in ("openai", "openai_compatible", "http"):
        if not target.get("base_url"):
            raise ValueError("target.type=openai 需要 base_url")
        gateway = OpenAICompatGateway(
            guard,
            store,
            base_url=target["base_url"],
            model=target.get("model", "target"),
            api_key_env=target.get("api_key_env", "OPENAI_API_KEY"),
            timeout=float(target.get("timeout_s", 60)),
        )
        gateway.stage_id = "target_query"
        return gateway
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


def build_controller(
    kind: str,
    *,
    queries: Optional[List[str]] = None,
    elements: Optional[List[str]] = None,
    gateway: Optional[ModelGateway] = None,
    renderer=None,
    stage_id: str = "plan",
    template=None,
    strategy: str = "model_query_v1",
    strategy_version: str = "",
) -> Controller:
    if kind == "scripted":
        return ScriptedController(queries or [])
    if kind == "element":
        return ElementController(elements or [])
    if kind == "llm":
        from ..controller import LLMController

        if gateway is None:
            raise ValueError("llm controller 需要 gateway")
        return LLMController(
            gateway,
            renderer,
            stage_id=stage_id,
            template=template,
            strategy=strategy,
            strategy_version=strategy_version,
        )
    raise ValueError(f"未知控制器：{kind}")


def no_assembly_budget_reason(non_target_left: int, asm_stage_left: int) -> str:
    """回退原因必须符合实际：尚有额度时不得标 no_local_budget。"""
    if asm_stage_left == 0 and non_target_left > 0:
        return "assembly_stage_exhausted"
    if non_target_left == 0 and asm_stage_left == 0:
        return "no_local_budget"
    if non_target_left == 0:
        return "no_non_target_budget"
    return "no_local_budget"


def run_episode(
    task: Task,
    *,
    config: Dict[str, Any],
    results_root: str,
    target_gateway: Optional[ModelGateway] = None,
    local_gateway: Optional[ModelGateway] = None,
    episode_id: Optional[str] = None,
    rubric: Optional[Rubric] = None,
    ctx=None,
) -> EpisodeArtifacts:
    """执行一条 episode。target_gateway 为空时按配置/路由构建。"""
    exp = config.get("experiment", {})
    method_cfg = config.get("method", {}) or {}
    routing: RoutingPlan = ctx.routing if ctx is not None else resolve_routing(config)
    limits: BudgetLimits = ctx.limits if ctx is not None else BudgetLimits(
        target_calls=int((exp.get("budget", {}) or {}).get("target_calls", 20)),
        non_target_calls=int((exp.get("budget", {}) or {}).get("non_target_calls", 400)),
    )
    prompts: Optional[PromptRegistry] = ctx.prompts if ctx is not None else (
        PromptRegistry.from_config(config) if config.get("prompts") else None
    )
    has_prompts = prompts is not None
    session_mode = SessionMode(exp.get("session_mode", "stateless"))
    state_cfg = method_cfg.get("state", {}) or {}
    assembly_cfg = method_cfg.get("assembly", {}) or {}
    search_cfg = method_cfg.get("search", {}) or {}
    recovery_cfg = method_cfg.get("recovery", {}) or {}
    stopping_cfg = method_cfg.get("stopping", {}) or {}
    stop_policy = str(stopping_cfg.get("policy", "after_assembly")).lower()
    if stop_policy not in ("after_assembly", "online"):
        raise ValueError(f"未支持的 stopping.policy：{stop_policy}（可选 after_assembly / online）")

    eid = episode_id or new_id("ep")
    store = EventStore(results_root, eid)
    guard = BudgetGuard(limits, store, eid)

    # ---- 角色网关：新注册表或冻结模板模式按路由构造 ----
    routed_local = routing.new_registry or has_prompts
    controller_route = routing.components.get("controller")
    controller_stage = controller_route.stage_id if controller_route is not None else "plan"
    assembler_route = routing.components.get("assembler")
    assembler_stage = assembler_route.stage_id if assembler_route is not None else "assemble"

    if target_gateway is None:
        if routing.new_registry or has_prompts:
            target_gateway = build_gateway(routing, "target", guard, store)
        else:
            target_gateway = build_target_gateway(config, task, guard, store)
    if local_gateway is None and not routing.new_registry and not has_prompts:
        local_gateway = build_local_gateway(config, guard, store)

    wheel_study = method_cfg.get("name") == wheel_release.METHOD
    if wheel_study:
        if (task.task_id != "wheel" or task.category != "repository_release"
                or not isinstance(target_gateway, MockGateway)
                or method_cfg.get("controller", {}).get("type") != "llm"
                or assembly_cfg.get("type") != "llm" or not has_prompts):
            raise ValueError("wheel-release@1 requires the wheel documentation task, a fixture and both model stages")
        target_gateway.scripts = _to_script_items(task.runtime_hints["target_scripts"])

    if rubric is None:
        from ..runner import resolve_rubric

        rubric = resolve_rubric(config, task)
    elements = rubric.elements or [{"key": e, "accept": [], "critical": True} for e in (task.runtime_hints or {}).get("elements", [])]
    allowed_state_keys = {"backend", "max_chars", "window_chars", "target_history"}
    unknown_state_keys = set(state_cfg) - allowed_state_keys
    if unknown_state_keys:
        raise ValueError(f"未支持的 method.state 配置：{sorted(unknown_state_keys)}")
    # 请求历史策略独立于传输会话模式（session_mode）：
    #   ancestor（默认）= 重放祖先历史给 Target（兼容既有实验条件）；
    #   isolated = 独立请求模式，Target 只见当前查询 + 固定背景 + 显式 system 提示。
    target_history_policy = str(state_cfg.get("target_history", "ancestor") or "ancestor").lower()
    if target_history_policy not in ("ancestor", "isolated"):
        raise ValueError(f"未支持的 method.state.target_history：{target_history_policy}（可选 ancestor / isolated）")
    # 跨分支材料合并策略：none=仅当前分支链（默认，既有条件）；all=装配包全观察节点去重合并
    cross_branch_merge = str(assembly_cfg.get("cross_branch_merge", "none") or "none").lower()
    if cross_branch_merge not in ("none", "all"):
        raise ValueError(f"未支持的 method.assembly.cross_branch_merge：{cross_branch_merge}（可选 none / all）")
    backend_name = state_cfg.get("backend", "fox")
    window_chars = state_cfg.get("window_chars")
    if window_chars is not None:
        if backend_name not in ("raw_history", "raw", "original"):
            raise ValueError("window_chars 仅适用于 raw_history 后端；其他后端请用 max_chars")
        window_chars = int(window_chars)
        if window_chars <= 0:
            raise ValueError("window_chars 必须为正整数")
    backend = make_backend(
        backend_name, elements, max_chars=int(state_cfg.get("max_chars", 400)), window_chars=window_chars
    )

    # ---- 装配器：显式选择模型装配或模板基线 ----
    assembly_kind = str(assembly_cfg.get("type") or "material_template").lower()
    assembler_gateway = None
    assembler_template = None
    if assembly_kind == "llm":
        assembler_template = prompts.get(assembler_stage) if prompts is not None else None
        if routed_local:
            assembler_gateway = build_gateway(routing, "assembler", guard, store, template_ref=(assembler_template.ref() if assembler_template else None))
        else:
            assembler_gateway = local_gateway
        # 引用定位修复需要行级文本：装配器先建，engine 起来后再回填（见下）
        assembler = make_assembler(config, gateway=assembler_gateway, template=assembler_template)
    else:
        assembler = Assembler()

    # Target 的 system 配置是实验条件，必须显式可查（hash 写入 episode_started）
    if "target_system_prompt" in config:
        target_system_prompt = str(config.get("target_system_prompt") or "")
    elif "system_prompt" in (config.get("target") or {}):
        target_system_prompt = str((config.get("target") or {}).get("system_prompt") or "")
    else:
        # 真实实验默认不注入额外系统指令；合成 baseline 保留旧的数据整理助手提示
        target_system_prompt = "" if str((exp.get("profile") or "")) == "real_dev" else DEFAULT_SYSTEM

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
        assembler=assembler,
        selector=make_selector(assembly_cfg.get("selector", "first")),
        judge_online=JudgeOnline(),
        rubric=rubric,
        search_policy=make_policy(search_cfg.get("policy", "linear")),
        scorer=make_scorer(search_cfg.get("scorer", "null")),
        elements=elements,
        k_candidates=int(assembly_cfg.get("k", 1)),
        branch_width=int(search_cfg.get("branch_width", 1)),
        stopping_policy=stop_policy,
        system_prompt=target_system_prompt,
        target_history_policy=target_history_policy,
        cross_branch_merge=cross_branch_merge,
    )
    engine.start()
    # citation-relocate@v1：注入行级文本（evidence.jsonl 属于 episode 目录）
    if assembly_kind == "llm" and getattr(assembler, "relocate_meta_rows", False):
        assembler.row_text_by_id = {r.get("evidence_id"): (r.get("text") or "")
                                    for r in engine.read_evidence_rows()
                                    if isinstance(r, dict) and r.get("evidence_id")}


    controller_cfg = method_cfg.get("controller", {}) or {}
    controller_kind = str(controller_cfg.get("type", "element")).lower()
    controller_template = None
    if controller_kind == "llm":
        controller_template = prompts.get(controller_stage) if prompts is not None else None
        if routed_local:
            controller_gateway = build_gateway(
                routing, "controller", guard, store, template_ref=(controller_template.ref() if controller_template else None)
            )
            engine.controller_gateway = controller_gateway
        else:
            controller_gateway = local_gateway or build_local_gateway(config, guard, store)
            engine.controller_gateway = controller_gateway
    else:
        controller_gateway = local_gateway
    if wheel_study:
        controller = wheel_release.WheelReleaseController(controller_gateway, controller_template, assembler)
    else:
        controller = build_controller(
            controller_kind,
            queries=controller_cfg.get("queries"),
            elements=elements,
            gateway=controller_gateway,
            renderer=backend.render_for_controller,
            stage_id=controller_stage,
            template=controller_template,
            strategy=str(controller_cfg.get("strategy") or "model_query_v1"),
            strategy_version=str(controller_cfg.get("strategy_version") or ""),
        )
    recovery = RecoveryPolicy(store, **{k: v for k, v in recovery_cfg.items() if k in ("max_network_retries", "max_format_retries", "total_cap", "version")})

    # ---- 请求产物索引（派生文件；权威记录是 request_generated 事件） ----
    requests_path = os.path.join(os.path.dirname(store.path), "requests.jsonl")
    request_ledger = RequestLedger(store, requests_path)
    last_request_id = ""

    max_steps = int(exp.get("max_steps", 12))
    steps = 0
    last_action: Optional[Action] = None
    pending_retry = False
    last_pack_hash = ""
    target_calls_at_last_pack = -1

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

    def _last_response_event_id(call_id: str) -> str:
        """按 call_id 找最近一条 response_received 的事件 ID（归因链用）。"""
        if not call_id:
            return ""
        for ev in reversed(store.events):
            if ev.type == "response_received" and ev.call_id == call_id:
                return ev.event_id
        return ""

    final_trigger_event_id = ""

    def _finalize(reason: str, *, trigger_event_id: str = "") -> None:
        """统一收尾：所有停止路径共用；装配判断后写唯一终止记录。

        判定顺序：
        1. 无候选，或（online 模式下）候选装配后材料已变化 → 需要装配；
        2. 需要装配且有材料：非 Target 总额度与 Assembler 阶段额度都够 → 模型装配；
           额度不足 → 不调用模型的原文回退，原因按实际缺口记录；
        3. 无材料且无候选：按既定规则不编造，直接终止；
        4. 候选存在且材料未变（或 after_assembly 首装即终）→ 保留旧候选，不重复装配。
        """
        node_id = engine.current_node_id
        snap = guard.snapshot()
        non_target_left = int(snap.get("non_target_calls_left", 0) or 0)
        # stage_left 为 None 表示该阶段未配置上限（只受总额约束），视为可用
        _asm_left = guard.stage_left(assembler_stage)
        asm_stage_left = 1 if _asm_left is None else int(_asm_left)
        # 材料判定用合并视图（与装配路径一致）：cross_branch_merge=all 时兄弟节点材料
        # 也应支撑收尾装配——只看当前节点会在"终止节点恰好无材料"时丢弃已采集证据。
        has_materials = bool(engine._build_pack_for(node_id)[0].materials)
        has_candidate = engine.final_candidate is not None
        # 4.6：候选新鲜度从最终选中候选自己的包追溯（Engine 共享助手，网页收尾同源）
        materials_changed = engine.candidate_materials_changed(node_id) if has_candidate else False
        need_assembly = (not has_candidate) or (stop_policy == "online" and materials_changed)
        if need_assembly and has_materials:
            if non_target_left > 0 and asm_stage_left > 0:
                try:
                    _assemble_now("finalize" if not has_candidate else "finalize_new_evidence")
                except BudgetExhausted:
                    _l = guard.stage_left(assembler_stage)
                    why = no_assembly_budget_reason(
                        int(guard.snapshot().get("non_target_calls_left", 0) or 0), 1 if _l is None else int(_l)
                    )
                    store.append(
                        "action_rejected",
                        role="engine",
                        node_id=node_id,
                        payload={"reason": "no_assembly_budget", "detail": why, "note": "改用不调用模型的原文回退"},
                    )
                    engine.assemble_without_model(why)
            else:
                why = no_assembly_budget_reason(non_target_left, asm_stage_left)
                store.append(
                    "action_rejected",
                    role="engine",
                    node_id=node_id,
                    payload={"reason": "no_assembly_budget", "detail": why, "note": "改用不调用模型的原文回退"},
                )
                engine.assemble_without_model(why)
        elif need_assembly and not has_materials:
            store.append(
                "action_rejected",
                role="engine",
                node_id=node_id,
                payload={"reason": "no_materials_to_assemble", "note": "无材料无候选，按既定规则收尾，不编造答案"},
            )
        elif has_candidate and materials_changed and stop_policy != "online":
            # after_assembly 首装即终：不应到达；防御性记录，不重复装配
            store.append(
                "action_rejected",
                role="engine",
                node_id=node_id,
                payload={"reason": "materials_changed_after_candidate", "note": "非 online 模式保留旧候选"},
            )
        # 归因链：由恢复决定触发的收尾，terminate 记录指向该决定事件
        engine.terminate(reason, trigger_event_id=trigger_event_id)

    final_reason = ""
    while not engine.terminated and steps < max_steps:
        node_id = engine.select_node() or engine.current_node_id
        state = engine.states[node_id]

        if pending_retry and last_action is not None:
            action = last_action  # R08：重试原动作，不推进控制器序列
            pending_retry = False
        else:
            _declared_this_step = None
            # requery-policy@v1 接线（配置 method.query_policy.type=requery_policy 启用；
            # 默认 none：不计算指令、prompt 与 payload 逐字节不变——A/B 臂不受影响）
            _policy_directive = None
            if str(((config.get("method") or {}).get("query_policy") or {}).get("type")
                   or "none") == "requery_policy":
                from ..requery_policy import directive_from_engine
                _policy_directive = directive_from_engine(engine, node_id, guard.snapshot())
            try:
                action = controller.propose(
                    task,
                    state,
                    elements=elements,
                    node_id=node_id,
                    view_id=engine.nodes[node_id].view_ref,
                    budget=guard.snapshot(),
                    can_assemble=True,
                    query_history=engine.query_history(node_id),
                    candidate_feedback=engine.latest_candidate_feedback(),
                    policy_directive=_policy_directive,
                )
                # plan.v6：控制器随动作声明的组件清单 → 状态持久（缺失时沿用上一份）
                _declared_this_step = getattr(action, "components", None) if action is not None else None
            except BudgetExhausted:
                # 阶段额度可能先于全局额度耗尽：统一收尾判断是否用剩余装配额度（N11 语义）
                stage_left = guard.stage_left(controller_stage)
                reason = "stage_budget_exhausted" if stage_left == 0 else "local_budget_exhausted"
                store.append(
                    "action_rejected",
                    role="controller",
                    stage_id=controller_stage,
                    node_id=node_id,
                    payload={"reason": reason, "stage_id": controller_stage, "stage_calls_left": stage_left},
                )
                final_reason = reason
                break

        if _declared_this_step is not None:
            state.components = [dict(c) for c in _declared_this_step]
            store.append(
                "components_declared",
                role="controller",
                node_id=node_id,
                payload={"components": [dict(c) for c in _declared_this_step], "action_id": action.action_id},
            )

        if action is None:
            # S07：保留组件真实状态（拒答 ≠ 格式错误）
            status = getattr(controller, "last_failure", None) or getattr(controller, "last_status", Status.INVALID_OUTPUT)
            if hasattr(controller, "last_call_id"):
                request_ledger.record_invalid(
                    task_id=task.task_id,
                    node_id=node_id,
                    view_id=getattr(controller, "last_view_id", ""),
                    reason=str(getattr(status, "value", status)),
                    generation_call_id=str(getattr(controller, "last_call_id", "") or ""),
                    template_ref=getattr(controller, "last_template_ref", {}) or {},
                )
            decision = recovery.handle(
                "controller", status, attempt_key="controller",
                trigger_event_id=_last_response_event_id(getattr(controller, "last_call_id", "") or ""),
                trigger_call_id=getattr(controller, "last_call_id", "") or "",
            )
            steps += 1
            if decision.action in ("retry_same", "retry_rephrase"):
                continue
            if decision.action == "fallback_assemble":
                _assemble_now(decision.reason)
                final_reason = "fallback_assembled"
                break
            final_reason = decision.reason
            break

        if action.type is ActionType.ASK_TARGET:
            q = action.params.get("query")
            if not isinstance(q, str) or not q.strip():
                # U03：空/非字符串查询属于格式失败，必须走有限恢复而不是反复执行
                decision = recovery.handle(
                    "controller", Status.INVALID_OUTPUT, attempt_key="controller",
                    trigger_event_id=_last_response_event_id(getattr(controller, "last_call_id", "") or ""),
                    trigger_call_id=getattr(controller, "last_call_id", "") or "",
                )
                store.append(
                    "action_rejected",
                    role="controller",
                    node_id=node_id,
                    payload={"reason": "empty_query", "action_id": action.action_id},
                )
                steps += 1
                if decision.action in ("retry_same", "retry_rephrase"):
                    continue
                if decision.action == "fallback_assemble":
                    _assemble_now(decision.reason)
                    final_reason = "fallback_assembled"
                    break
                final_reason = decision.reason
                break
            # 请求产物：模型生成的有效查询才发送；同一 query 的重发复用同一 request_id
            if not action.params.get("request_id"):
                parent = last_request_id
                artifact = request_ledger.record(
                    task_id=task.task_id,
                    query=q,
                    node_id=node_id,
                    view_id=engine.nodes[node_id].view_ref,
                    strategy=str(controller_cfg.get("strategy") or (getattr(controller, "strategy", "") or "scripted")),
                    strategy_version=str(
                        getattr(controller, "strategy_version", "")
                        or controller_cfg.get("strategy_version", "")
                        or "model_query_v1@1"
                    ),
                    parent_request_ids=[parent] if parent else [],
                    generation_call_id=str(getattr(controller, "last_call_id", "") or ""),
                    template_ref=getattr(controller, "last_template_ref", {}) or {},
                    input_evidence_ids=getattr(controller, "last_input_evidence_ids", []) or [],
                    parse_status="parsed",
                    rationale=action.rationale,
                    transform_chain=[str(controller_cfg.get("strategy") or "scripted")],
                    # requery-policy@v1：策略指令与搭车抽取随权威事件落盘（可选）
                    policy_directive=(
                        _policy_directive.to_dict() if _policy_directive is not None
                        and hasattr(_policy_directive, "to_dict") else None),
                    delivered=getattr(controller, "last_delivered", None),
                )
                action.params["request_id"] = artifact.request_id
                last_request_id = artifact.request_id

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
            final_reason = "budget_exhausted"
            break  # R09：交给统一收尾，用剩余本地额度装配

        if result.rejected and result.status is Status.INVALID_OUTPUT:
            decision = recovery.handle("controller", Status.INVALID_OUTPUT, attempt_key="engine_reject")
            if decision.action in ("retry_same", "retry_rephrase"):
                continue
            if decision.action == "fallback_assemble":
                _assemble_now(decision.reason)
                final_reason = "fallback_assembled"
                break
            final_reason = decision.reason
            break

        if result.retryable and not result.rejected:
            decision = recovery.handle(
                "target", result.status, attempt_key="target",
                trigger_event_id=getattr(result, "response_event_id", "") or "",
                trigger_call_id=getattr(result, "call_id", "") or "",
            )
            if decision.action in ("retry_same", "retry_rephrase") and guard.can_spend_target():
                pending_retry = True
                continue
            if decision.action == "fallback_assemble":
                _assemble_now(decision.reason)
                final_reason = "fallback_assembled"
                final_trigger_event_id = decision.event_id
                break
            if decision.action == "stop":
                final_reason = decision.reason
                final_trigger_event_id = decision.event_id
                break

        if getattr(engine, "stop_action_requested", False):
            final_reason = "stop_action"
            break

        if action.type is ActionType.ASSEMBLE and engine.final_candidate is not None:
            if engine.stop_reason:
                engine.terminate("online_success")
                break
            if stop_policy == "after_assembly":
                engine.terminate("assembled")
                break
            # online：候选已冻结，但允许继续取证；材料与调用都没变时重复装配视为无进展
            pack_hash = (engine.packs[-1].get("input_hash") if engine.packs else "") or ""
            target_calls = guard.snapshot().get("target_calls_used", 0)
            if pack_hash and pack_hash == last_pack_hash and target_calls == target_calls_at_last_pack:
                engine.terminate("no_progress_assembly")
                break
            last_pack_hash, target_calls_at_last_pack = pack_hash, target_calls

    if not engine.terminated:
        # 统一收尾：五个停止路径（STOP / max_steps / Target 额度耗尽 / Controller 阶段额度耗尽 /
        # 恢复策略停止）都汇到这里；V01 的 online 新证据重装配语义包含在 _finalize 内。
        _finalize(final_reason or "max_steps", trigger_event_id=final_trigger_event_id)

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
