"""执行引擎（手册 §4、§12.1）。

职责：校验动作、执行查询或装配、维护节点/视图/状态、记账、写事件。
不负责：提出动作、最终评价、结果落定。

本版修复（review a9b45c9）：
- R06：Evidence 完整记录落盘（evidence.jsonl），引用可解析到原始事件与 span；
- R07：state_write 保存所属节点与完整快照，恢复按节点取快照；
- R09：Target 额度耗尽不再直接终止，留给编排层用剩余本地额度装配；
- R11：分支宽度与 UCT 访问/回传接通。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .assembly import Assembler, CandidateSelector
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
from .state import StateBackend, compute_open, normalize_element, snapshot_state

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
    retryable: bool = False


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
        elements: Optional[List[Any]] = None,
        k_candidates: int = 1,
        branch_width: int = 1,
        stopping_policy: str = "after_assembly",
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
        self.elements: List[Dict[str, Any]] = [normalize_element(e) for e in (elements or [e for e in rubric.elements])]
        self.k_candidates = k_candidates
        self.branch_width = max(1, branch_width)
        self.stopping_policy = stopping_policy
        self.system_prompt = system_prompt

        self.nodes: Dict[str, SearchNode] = {}
        self.states: Dict[str, AgentState] = {}
        self.views: Dict[str, HistoryView] = {}
        self.children: Dict[str, List[str]] = {}
        self.frontier = Frontier()
        self.current_node_id = ""
        self.final_candidate: Optional[AnswerCandidate] = None
        self.candidates: List[AnswerCandidate] = []
        self.packs: List[Dict[str, Any]] = []
        self.terminated = False
        self.termination_reason = ""
        self.stop_reason = ""
        self.target_exhausted = False
        self.attempt_counter = 0
        self._evidence_dir = os.path.dirname(store.path)

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
                "rubric": self.rubric.to_dict(),
                "method_version": self.episode.method_version,
                "budget": self.episode.budget.to_dict(),
                "session_mode": self.episode.session_mode.value,
                "stopping_policy": self.stopping_policy,
                "config_hash": self.episode.config_hash,
                "model": getattr(self.target, "model_name", ""),
            },
            budget_after=self.guard.snapshot(),
        )
        view = HistoryView(view_id=new_id("view"), parent_view_id=None, visible_message_refs=[], session_mode=self.episode.session_mode)
        self.views[view.view_id] = view
        state = AgentState(budget_left=self.guard.snapshot())
        self.states["root"] = state
        root = SearchNode(node_id="root", parent_node_id=None, state_snapshot_ref="root", view_ref=view.view_id)
        self.nodes["root"] = root
        self.children["root"] = []
        self.frontier.add("root", None)
        self.store.append("node_created", role="engine", node_id="root", view_id=view.view_id, payload={"parent": None, "depth": 0})
        self.store.append("frontier_updated", role="engine", payload={"added": ["root"]})
        self.current_node_id = "root"
        return "root"

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
            # R09：不终止；标记后交给编排层用剩余本地额度装配
            self.target_exhausted = True
            self.store.append(
                "action_rejected",
                role="engine",
                node_id=node_id,
                payload={"action_id": action.action_id, "type": "ASK_TARGET", "reason": "target_budget_exhausted"},
            )
            return StepResult(action.action_id, Status.BUDGET_EXHAUSTED, "target 预算耗尽，禁止新增请求", node_id=node_id)

        messages = self._build_messages(view, query)
        self.store.append(
            "view_snapshot",
            role="engine",
            node_id=node_id,
            view_id=view.view_id,
            payload={"messages": messages, "session_mode": view.session_mode.value},
        )
        self.attempt_counter += 1
        res = self.target.call(
            Role.TARGET,
            stage_id="target_query",
            messages=messages,
            params=action.params.get("params") or {},
            node_id=node_id,
            view_id=view.view_id,
            attempt_id=f"att_{self.attempt_counter}",
        )

        response_ev = self.store.last("response_received")
        source_event_id = res.response_event_id or (response_ev.event_id if response_ev else "")
        evidence = self._make_evidence(source_event_id, res.text, node_id=node_id, call_id=res.call_id)
        label = label_target(res.text)
        if label.value in ("refusal", "soft_refusal"):
            self.store.append(
                "target_refusal",
                role="target",
                node_id=node_id,
                call_id=res.call_id,
                payload={"label": label.value, "excerpt": res.text[:200]},
            )

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
        new_state.parent_state_seq = self.states[node_id].source_event_seq
        new_state.extractor_version = getattr(self.backend, "name", "") + "@v1"

        child_id = new_id("n")
        new_state.node_id = child_id
        new_state.open = compute_open(new_state, self.elements)

        child_view = HistoryView(
            view_id=new_id("view"),
            parent_view_id=view.view_id,
            visible_message_refs=list(view.visible_message_refs)
            + [e.event_id for e in (self.store.last("request_sent"), response_ev) if e],
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
        self.children.setdefault(child_id, [])
        self.states[child_id] = new_state
        self.children.setdefault(node_id, []).append(child_id)

        # R07：state_write 带节点与完整快照
        self.store.append(
            "state_write",
            role="extractor",
            node_id=child_id,
            payload={
                **update.to_dict(),
                "backend": self.backend.name,
                "state_seq": new_state.source_event_seq,
                "node": child_id,
                "parent_node": node_id,
                "parent_state_seq": new_state.parent_state_seq,
                "extractor_version": new_state.extractor_version,
                "snapshot": snapshot_state(new_state),
            },
        )
        self.store.append(
            "node_created",
            role="engine",
            node_id=child_id,
            view_id=child_view.view_id,
            payload={"parent": node_id, "query": query, "status": res.status.value, "label": label.value},
        )

        score = self.scorer.score(self.task, new_state, child, res.text, new_facts=len(update.new_facts))
        child.score_ref = score.ref
        self.store.append("score_recorded", role="scorer", node_id=child_id, payload=score.to_dict())
        self.frontier.add(child_id, score.score)
        reward = score.score if score.score is not None else 0.0
        self.frontier.record_visit(child_id, reward=reward)
        # 平均回传：一次完成的扩展对其路径上每个祖先各贡献一次访问与同一 reward
        ancestor = parent.node_id if parent is not None else None
        while ancestor:
            node = self.nodes.get(ancestor)
            if node is None:
                break
            self.frontier.record_visit(ancestor, reward=reward)
            ancestor = node.parent_node_id

        # R11：分支宽度 > 1 时父节点保留在前沿，可再扩展出兄弟
        keep_parent = self.branch_width > 1 and len(self.children.get(node_id, [])) < self.branch_width
        if not keep_parent:
            self.frontier.remove(node_id)
        self.store.append(
            "frontier_updated",
            role="engine",
            payload={"added": [child_id], "removed": [] if keep_parent else [node_id], "branch_width": self.branch_width},
        )
        self.current_node_id = child_id

        status = res.status
        retryable = res.status in (Status.TIMEOUT, Status.TRANSPORT_ERROR, Status.INVALID_OUTPUT)
        return StepResult(action.action_id, status, f"target 调用完成：{label.value}", node_id=child_id, retryable=retryable)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _freeze_pack(self, pack, node_id: str, state):
        """深拷贝材料并落盘；返回冻结后的包（输入不可被后续状态改写）。"""
        from .assembly import EvidencePack

        materials = json.loads(json.dumps(pack.materials, ensure_ascii=False, default=str))
        frozen = EvidencePack(
            pack_id=pack.pack_id,
            task_id=pack.task_id,
            materials=materials,
            gaps=list(pack.gaps),
            notes=list(pack.notes),
        )
        row = {
            "pack_id": frozen.pack_id,
            "task_id": frozen.task_id,
            "materials": materials,
            "gaps": frozen.gaps,
            "notes": frozen.notes,
            "view": {"backend": self.backend.name, "state_seq": state.source_event_seq, "node_id": node_id},
            "input_hash": hashlib.sha256(
                json.dumps(materials, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest(),
        }
        self.packs.append(row)
        with open(os.path.join(self._evidence_dir, "packs.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self.store.append(
            "pack_frozen",
            role="assembler",
            node_id=node_id,
            payload={"pack_id": frozen.pack_id, "input_hash": row["input_hash"], "materials": len(materials), "gaps": frozen.gaps},
        )
        return frozen

    def _link_pack_candidates(self, pack, candidates) -> None:
        row = {"pack_id": pack.pack_id, "candidate_ids": [c.candidate_id for c in candidates]}
        with open(os.path.join(self._evidence_dir, "pack_links.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _assemble(self, action: Action) -> StepResult:
        err = self._validate(action)
        if err:
            return self._reject(action, err)
        node_id = action.node_id or self.current_node_id
        state = self.states[node_id]
        built = self.assembler.build_pack(self.task, state, self.elements)
        # T06：先冻结并落盘装配输入，再调用装配器；候选关联另记，不改写已冻结的包定义
        frozen = self._freeze_pack(built, node_id, state)
        candidates = self.assembler.assemble_from_pack(
            self.task,
            frozen,
            k=int(action.params.get("k") or self.k_candidates),
            stage_policy_ref=self.episode.stage_policy_ref,
        )
        self.candidates.extend(candidates)
        self._link_pack_candidates(frozen, candidates)

        self.store.append(
            "assembled",
            role="assembler",
            node_id=node_id,
            payload={
                "k": len(candidates),
                "candidates": [
                    {"id": c.candidate_id, "chars": len(c.answer), "gaps": c.gaps, "claims": len(c.claims)} for c in candidates
                ],
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

    def _make_evidence(self, event_id: str, text: str, *, node_id: str = "", call_id: str = "") -> List[Evidence]:
        """按行建立 Evidence，并立即落盘（R06）。"""
        out: List[Evidence] = []
        pos = 0
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\n")
            start = pos
            pos += len(line)
            if not body.strip():
                continue
            ev = Evidence.from_text(event_id, "assistant", text, start, start + len(body))
            record = {
                "evidence_id": ev.evidence_id,
                "source_event_id": event_id,
                "call_id": call_id,
                "node_id": node_id,
                "role": ev.role,
                "text": ev.text,
                "span_start": ev.span_start,
                "span_end": ev.span_end,
                "checksum": ev.checksum,
            }
            with open(os.path.join(self._evidence_dir, "evidence.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.append(ev)
        return out

    # ------------------------------------------------------------------
    def select_node(self) -> Optional[str]:
        budget_left = self.guard.snapshot().get("target_calls_left")
        nid = self.policy.select(self.frontier, self.nodes, budget_left)
        if nid:
            self.current_node_id = nid
            # 选择不是样本：不在这里增加访问次数（否则会稀释均值）
            self.store.append(
                "node_selected",
                role="engine",
                node_id=nid,
                payload={"policy": self.policy.name, "visits": self.frontier.visits.get(nid, 0)},
            )
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
