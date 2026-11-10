"""执行引擎（手册 §4、§12.1）。

职责：校验动作、执行查询或装配、维护节点/视图/状态、记账、写事件。
不负责：提出动作（Controller）、决定停止策略（SearchPolicy/Judge_online 的配置）、最终评价（JudgeEval）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .assembly import Assembler, CandidateSelector, EvidencePack
from .budget import BudgetGuard
from .clients import ModelGateway
from .contracts import (
    Action,
    ActionType,
    AgentState,
    AnswerCandidate,
    Episode,
    Evidence,
    HistoryView,
    NodeStatus,
    Role,
    SearchNode,
    SessionMode,
    Status,
    Task,
    new_id,
)
from .events import EventStore
from .judge import JudgeOnline, Rubric, label_target
from .search import Frontier, Scorer, SearchPolicy
from .state import StateBackend

DEFAULT_SYSTEM = (
    "你是资料整理助手。只依据对话中出现的内容作答；"
    "不确定的内容写“未提供”，不要补充对话之外的信息。"
)


@dataclass
class StepResult:
    action_id: str
    status: Status
    message: str = ""
    node_id: str = ""
    rejected: bool = False


class Engine:
    def __init__(
        self,
        *,
        task: Task,
        episode: Episode,
        store: EventStore,
        guard: BudgetGuard,
        target_gateway: ModelGateway,
        state_backend: StateBackend,
        assembler: Assembler,
        selector: CandidateSelector,
        judge_online: JudgeOnline,
        rubric: Rubric,
        search_policy: SearchPolicy,
        scorer: Scorer,
        elements: Optional[List[str]] = None,
        k_candidates: int = 1,
        system_prompt: str = DEFAULT_SYSTEM,
    ):
        self.task = task
        self.episode = episode
        self.store = store
        self.guard = guard
        self.target = target_gateway
        self.backend = state_backend
        self.assembler = assembler
        self.selector = selector
        self.judge_online = judge_online
        self.rubric = rubric
        self.policy = search_policy
        self.scorer = scorer
        self.elements = elements or [e["key"] for e in rubric.elements]
        self.k_candidates = k_candidates
        self.system_prompt = system_prompt

        self.nodes: Dict[str, SearchNode] = {}
        self.states: Dict[str, AgentState] = {}
        self.views: Dict[str, HistoryView] = {}
        self.frontier = Frontier()
        self.current_node_id: str = ""
        self.final_candidate: Optional[AnswerCandidate] = None
        self.candidates: List[AnswerCandidate] = []
        self.terminated = False
        self.termination_reason: str = ""
        self.stop_reason: str = ""

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self) -> str:
        self.store.append(
            "episode_started",
            role="engine",
            payload={
                "task_id": self.task.task_id,
                "prompt": self.task.prompt,
                "category": self.task.category,
                "elements": self.elements,
                "method_version": self.episode.method_version,
                "budget": self.episode.budget.to_dict(),
                "session_mode": self.episode.session_mode.value,
                "config_hash": self.episode.config_hash,
            },
            budget_after=self.guard.snapshot(),
        )
        view = HistoryView(view_id=new_id("view"), parent_view_id=None, visible_message_refs=[], session_mode=self.episode.session_mode)
        self.views[view.view_id] = view
        state = AgentState(budget_left=self.guard.snapshot())
        self.states["root"] = state
        root = SearchNode(node_id="root", parent_node_id=None, state_snapshot_ref="root", view_ref=view.view_id)
        self.nodes["root"] = root
        self.frontier.add("root", None)
        self.store.append(
            "node_created",
            role="engine",
            node_id="root",
            view_id=view.view_id,
            payload={"parent": None, "depth": 0},
        )
        self.store.append("frontier_updated", role="engine", payload={"added": ["root"]})
        self.current_node_id = "root"
        return "root"

    # ------------------------------------------------------------------
    # 动作执行
    # ------------------------------------------------------------------
    def execute(self, action: Action) -> StepResult:
        if self.terminated:
            return StepResult(action.action_id, Status.INVALID_OUTPUT, "episode 已终止", rejected=True)
        if action.type is ActionType.ASK_TARGET:
            return self._ask_target(action)
        if action.type is ActionType.ASSEMBLE:
            return self._assemble(action)
        if action.type is ActionType.STOP:
            self.terminate("stop_action")
            return StepResult(action.action_id, Status.OK, "stopped")
        return self._reject(action, f"未知动作类型：{action.type}")

    # ------------------------------------------------------------------
    def _reject(self, action: Action, reason: str) -> StepResult:
        self.store.append(
            "action_rejected",
            role="engine",
            node_id=action.node_id or self.current_node_id,
            payload={"action_id": action.action_id, "type": str(action.type), "reason": reason},
        )
        return StepResult(action.action_id, Status.INVALID_OUTPUT, reason, rejected=True)

    def _validate(self, action: Action) -> Optional[str]:
        node_id = action.node_id or self.current_node_id
        if node_id not in self.nodes:
            return f"未知节点：{node_id}"
        state = self.states.get(node_id)
        if state is None:
            return f"节点缺少状态：{node_id}"
        if action.based_on_state_seq and action.based_on_state_seq != state.source_event_seq:
            return f"状态版本过期：action={action.based_on_state_seq} state={state.source_event_seq}"
        view_id = action.view_id or self.nodes[node_id].view_ref
        if view_id not in self.views:
            return f"未知视图：{view_id}"
        return None

    # ------------------------------------------------------------------
    def _ask_target(self, action: Action) -> StepResult:
        err = self._validate(action)
        if err:
            return self._reject(action, err)
        node_id = action.node_id or self.current_node_id
        parent = self.nodes[node_id]
        view = self.views[action.view_id or parent.view_ref]
        query = str(action.params.get("query", "")).strip()
        if not query:
            return self._reject(action, "query 为空")

        if not self.guard.can_spend_target():
            self.terminate("budget_exhausted")
            return StepResult(action.action_id, Status.BUDGET_EXHAUSTED, "target 预算耗尽")

        messages = self._build_messages(view, query)
        self.store.append(
            "view_snapshot",
            role="engine",
            node_id=node_id,
            view_id=view.view_id,
            payload={"messages": messages, "session_mode": view.session_mode.value},
        )
        res = self.target.call(
            Role.TARGET,
            stage_id="target_query",
            messages=messages,
            params=action.params.get("params") or {},
            node_id=node_id,
            view_id=view.view_id,
        )

        # 观察落盘：证据按行定位
        response_ev = self.store.last("response_received")
        evidence = self._make_evidence(response_ev.event_id if response_ev else "", res.text)
        label = label_target(res.text)
        if label.value in ("refusal", "soft_refusal"):
            self.store.append(
                "target_refusal",
                role="target",
                node_id=node_id,
                call_id=res.call_id,
                payload={"label": label.value, "excerpt": res.text[:200]},
            )

        # 状态更新
        new_state = copy.deepcopy(self.states[node_id])
        update = self.backend.apply(
            self.task,
            new_state,
            response_text=res.text,
            evidence=evidence,
            status=res.status,
            event_seq=(response_ev.seq if response_ev else 0),
        )
        new_state.budget_left = self.guard.snapshot()
        self.store.append(
            "state_write",
            role="extractor",
            node_id=node_id,
            payload={**update.to_dict(), "state_seq": new_state.source_event_seq, "backend": self.backend.name},
        )

        # 子节点与视图
        child_id = new_id("n")
        child_view = HistoryView(
            view_id=new_id("view"),
            parent_view_id=view.view_id,
            visible_message_refs=list(view.visible_message_refs) + [e.event_id for e in (self.store.last("request_sent"), response_ev) if e],
            session_mode=view.session_mode,
        )
        self.views[child_view.view_id] = child_view
        child = SearchNode(
            node_id=child_id,
            parent_node_id=node_id,
            state_snapshot_ref=child_id,
            view_ref=child_view.view_id,
            action_ref=action.action_id,
            response_ref=response_ev.event_id if response_ev else None,
            status=NodeStatus.OPEN,
        )
        self.nodes[child_id] = child
        self.states[child_id] = new_state
        self.store.append(
            "node_created",
            role="engine",
            node_id=child_id,
            view_id=child_view.view_id,
            payload={"parent": node_id, "query": query, "status": res.status.value, "label": label.value},
        )

        # 评分与前沿
        score = self.scorer.score(self.task, new_state, child, res.text, new_facts=len(update.new_facts))
        child.score_ref = score.ref
        self.store.append(
            "score_recorded",
            role="scorer",
            node_id=child_id,
            payload=score.to_dict(),
        )
        self.frontier.add(child_id, score.score)
        self.frontier.remove(node_id)
        self.store.append("frontier_updated", role="engine", payload={"added": [child_id], "removed": [node_id]})
        self.current_node_id = child_id

        status = res.status if res.ok else res.status
        return StepResult(action.action_id, status, f"target 调用完成：{label.value}", node_id=child_id)

    # ------------------------------------------------------------------
    def _assemble(self, action: Action) -> StepResult:
        err = self._validate(action)
        if err:
            return self._reject(action, err)
        node_id = action.node_id or self.current_node_id
        state = self.states[node_id]
        candidates = self.assembler.assemble(
            self.task,
            state,
            k=int(action.params.get("k") or self.k_candidates),
            elements=self.elements,
            stage_policy_ref=self.episode.stage_policy_ref,
        )
        self.candidates.extend(candidates)
        self.store.append(
            "assembled",
            role="assembler",
            node_id=node_id,
            payload={
                "k": len(candidates),
                "candidates": [{"id": c.candidate_id, "chars": len(c.answer), "gaps": c.gaps} for c in candidates],
            },
        )
        chosen = self.selector.select(candidates)
        if chosen is None:
            return StepResult(action.action_id, Status.INVALID_OUTPUT, "无可选候选", rejected=True)
        self.final_candidate = chosen
        self.store.append(
            "candidate_selected",
            role="selector",
            node_id=node_id,
            payload={"candidate_id": chosen.candidate_id, "selector": self.selector.name},
        )
        verdict, ratio, why = self.judge_online.check(self.task, chosen.answer, self.rubric)
        self.store.append(
            "online_verdict",
            role="judge_online",
            node_id=node_id,
            payload={"decision": verdict, "ratio": ratio, "basis": why},
        )
        if verdict == "stop":
            self.stop_reason = f"online_success:{why}"
        return StepResult(action.action_id, Status.OK, f"装配完成：{verdict}（{why}）")

    # ------------------------------------------------------------------
    def _build_messages(self, view: HistoryView, query: str) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = [{"role": "system", "content": self.system_prompt}]
        if self.task.necessary_context:
            msgs.append({"role": "user", "content": f"背景资料：{self.task.necessary_context}"})
        by_id = {e.event_id: e for e in self.store.events}
        for ref in view.visible_message_refs:
            ev = by_id.get(ref)
            if ev is None:
                continue
            if ev.type == "request_sent":
                msgs.append({"role": "user", "content": ev.payload.get("messages", [])[-1].get("content", "")})
            elif ev.type == "response_received":
                msgs.append({"role": "assistant", "content": ev.payload.get("text", "")})
        if not msgs or msgs[-1].get("content") != query:
            msgs.append({"role": "user", "content": query})
        return msgs

    def _make_evidence(self, event_id: str, text: str) -> List[Evidence]:
        out: List[Evidence] = []
        pos = 0
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\n")
            start = pos
            pos += len(line)
            if not body.strip():
                continue
            out.append(Evidence.from_text(event_id, "assistant", text, start, start + len(body)))
        return out

    # ------------------------------------------------------------------
    def select_node(self) -> Optional[str]:
        budget_left = self.guard.snapshot().get("target_calls_left")
        nid = self.policy.select(self.frontier, self.nodes, budget_left)
        if nid:
            self.current_node_id = nid
            self.store.append("node_selected", role="engine", node_id=nid, payload={"policy": self.policy.name})
        return nid

    def terminate(self, reason: str) -> None:
        if self.terminated:
            return
        self.terminated = True
        self.termination_reason = reason
        self.store.append(
            "terminate",
            role="engine",
            node_id=self.current_node_id,
            payload={
                "reason": reason,
                "final_candidate_id": self.final_candidate.candidate_id if self.final_candidate else None,
                "stop_reason": self.stop_reason,
            },
            budget_after=self.guard.snapshot(),
        )
