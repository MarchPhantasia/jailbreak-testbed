"""状态表示与恢复（手册 §5.2、§6、§13）。

三种后端共享同一接口，差异只在"观察如何进入状态"：

- RawHistoryBackend：不做抽取；状态里只有事件引用，原文从事件流读。
- SummaryBackend：把观察压成等长受限的摘要（可用规则或模型）。
- FoxBackend：抽取 F（facts）/ O（open）/ X（failed），每条事实带出处与保真标记。

共同约束：
- 事实必须带 evidence_ids；
- 无出处的内容不能进 facts（只能进 hypotheses）；
- 条件、否定、版本保留在文本里（不做自由补全）；
- 恢复只应用已记录的 state_write，不调用模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (
    AgentState,
    Evidence,
    Fidelity,
    Origin,
    Proposition,
    Status,
    SupportStatus,
    Task,
    new_id,
)

LINE_RE = re.compile(r"^\s*[-*]?\s*([^\s:：]{1,16})\s*[:：]\s*(.+?)\s*$")


@dataclass
class StateUpdate:
    """一次观察对状态的影响，落成 state_write 事件。"""

    backend: str
    new_facts: List[Dict[str, Any]] = field(default_factory=list)
    removed_open: List[str] = field(default_factory=list)
    new_open: List[str] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "new_facts": self.new_facts,
            "removed_open": self.removed_open,
            "new_open": self.new_open,
            "failed": self.failed,
            "summary": self.summary,
            "notes": self.notes,
        }


class StateBackend:
    name = "base"

    def __init__(self, elements: Optional[List[str]] = None):
        #: 任务要求要素（用于 O 的计算）；为空表示不追踪缺项
        self.elements: List[str] = elements or []

    # ---- 接口 ----
    def apply(
        self,
        task: Task,
        state: AgentState,
        *,
        response_text: str,
        evidence: List[Evidence],
        status: Status,
        event_seq: int,
        extractor_ref: str = "rule@v1",
    ) -> StateUpdate:  # pragma: no cover - 抽象
        raise NotImplementedError

    def render_for_controller(self, task: Task, state: AgentState) -> str:
        return state.summary or "；".join(state.fact_texts())

    # ---- 共用 ----
    def _missing_elements(self, state: AgentState) -> List[str]:
        have = " ".join(state.fact_texts())
        return [e for e in self.elements if e not in have]


class RawHistoryBackend(StateBackend):
    """原文组：不抽取事实，只记录"看过哪些事件"。"""

    name = "raw_history"

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        update = StateUpdate(backend=self.name)
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR):
            update.failed.append({"reason": status.value, "event_seq": event_seq})
        state.source_event_seq = event_seq
        return update

    def render_for_controller(self, task: Task, state: AgentState) -> str:
        return f"（原文历史，共 {state.source_event_seq} 条事件）"


class SummaryBackend(StateBackend):
    """摘要组：把观察压成受限长度摘要；长度上限由配置给出（等长对照用）。"""

    name = "summary"

    def __init__(self, elements: Optional[List[str]] = None, max_chars: int = 400):
        super().__init__(elements)
        self.max_chars = max_chars

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        update = StateUpdate(backend=self.name)
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR):
            update.failed.append({"reason": status.value, "event_seq": event_seq, "text": response_text[:120]})
        snippet = response_text.strip().replace("\n", " ")
        merged = (state.summary + " " + snippet).strip() if state.summary else snippet
        state.summary = merged[-self.max_chars :]
        state.source_event_seq = event_seq
        update.summary = state.summary
        update.notes.append("summary 截断为等长对照长度")
        return update


class FoxBackend(StateBackend):
    """F/O/X 组：按键值行抽取命题，保留出处与保真标记，维护缺项与失败方向。"""

    name = "fox"

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        update = StateUpdate(backend=self.name)
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR):
            update.failed.append(
                {
                    "reason": status.value,
                    "event_seq": event_seq,
                    "excerpt": response_text[:160],
                    "scope": "episode",
                }
            )
            state.failed.append(update.failed[-1])
            state.source_event_seq = event_seq
            return update

        by_text = {e.text: e.evidence_id for e in evidence}
        for line in response_text.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            if not value:
                continue
            evid = None
            for text, eid in by_text.items():
                if value in text:
                    evid = eid
                    break
            if evid is None:
                # 没有可定位出处的内容只能进 hypotheses
                state.hypotheses.append({"text": f"{key}: {value}", "reason": "no_evidence"})
                update.notes.append(f"{key} 无出处，记为 hypothesis")
                continue
            prop = Proposition(
                fact_id=new_id("f"),
                text=value,
                origin=Origin.TARGET_RESPONSE,
                evidence_ids=[evid],
                fidelity=Fidelity.VERBATIM,
                support_status=SupportStatus.ENTAILED,
                qualifiers={"key": key},
                extractor_ref=extractor_ref,
            )
            if any(p.text == prop.text for p in state.facts):
                continue
            state.facts.append(prop)
            update.new_facts.append({"fact_id": prop.fact_id, "key": key, "text": prop.text, "evidence_ids": prop.evidence_ids})

        missing = self._missing_elements(state)
        state.open = [{"goal": e, "why_open": "not_seen"} for e in missing]
        update.new_open = missing
        state.source_event_seq = event_seq
        return update

    def render_for_controller(self, task: Task, state: AgentState) -> str:
        parts = ["已知事实："]
        parts += [f"- {f.qualifiers.get('key', '')}: {f.text}" for f in state.facts] or ["- （空）"]
        parts.append("缺项：" + ("、".join(o["goal"] for o in state.open) if state.open else "无"))
        if state.failed:
            parts.append("失败方向：" + "；".join(f"{x.get('reason')}" for x in state.failed[:3]))
        return "\n".join(parts)


def make_backend(name: str, elements: Optional[List[str]] = None, max_chars: int = 400) -> StateBackend:
    name = (name or "raw_history").lower()
    if name in ("raw", "raw_history", "original"):
        return RawHistoryBackend(elements)
    if name == "summary":
        return SummaryBackend(elements, max_chars=max_chars)
    if name in ("fox", "f_o_x", "structured"):
        return FoxBackend(elements)
    raise ValueError(f"未知状态后端：{name}")


def restore_state(store, backend: StateBackend, task: Task) -> AgentState:
    """按已记录的 state_write 重放状态；不调用任何模型。"""
    state = AgentState()
    for ev in store.of_type("state_write"):
        payload = ev.payload or {}
        for fact in payload.get("new_facts", []):
            state.facts.append(
                Proposition(
                    fact_id=fact.get("fact_id") or new_id("f"),
                    text=fact["text"],
                    origin=Origin.TARGET_RESPONSE,
                    evidence_ids=list(fact.get("evidence_ids") or []),
                    fidelity=Fidelity.VERBATIM,
                    support_status=SupportStatus.ENTAILED,
                    qualifiers={"key": fact.get("key", "")},
                )
            )
        for item in payload.get("new_open", []) or []:
            state.open.append({"goal": item, "why_open": "not_seen"})
        for item in payload.get("failed", []) or []:
            state.failed.append(item)
        if payload.get("summary"):
            state.summary = payload["summary"]
        state.source_event_seq = ev.seq
    return state
