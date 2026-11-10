"""状态表示、材料与恢复（手册 §5.2、§6、§13）。

三种后端共享同一接口，差异在"观察如何进入状态与材料"：

- RawHistoryBackend：不抽事实；把每轮原文按行保存为材料（assembly 直接消费）。
- SummaryBackend：把观察压成受限长度摘要；材料是摘要本身及其加工引用。
- FoxBackend：抽取 F/O/X；材料是命题（带 evidence）。

共同约束：
- 材料必须带 evidence_ids；
- 无出处的内容只能进 hypotheses；
- 条件、否定、版本保留在文本里；"未确认 / 未提供 / 旧版"不算回答；
- 恢复按节点取最近快照，不合并兄弟分支，不调用模型。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
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
FLAT_LINE_RE = re.compile(r"([^\s:：]{1,16})\s*[:：]\s*([^\s:：]{1,80}?)(?=\s+[^\s:：]{1,16}\s*[:：]|$)")
#: 出现这些标记的值不算"已回答该要素"
UNANSWERED_MARKERS = ("未确认", "未提供", "未知", "未列出", "尚未", "不确定", "not confirmed", "unknown", "tbd")
STALE_MARKERS = ("旧版", "v1 ", "旧目录", "deprecated")


@dataclass
class StateUpdate:
    backend: str
    new_facts: List[Dict[str, Any]] = field(default_factory=list)
    removed_open: List[str] = field(default_factory=list)
    new_open: List[str] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    materials: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# 要素与覆盖
# --------------------------------------------------------------------------
def normalize_element(el: Any) -> Dict[str, Any]:
    """要素声明：字符串 → 只要求"该 key 被回答"；字典可带显式 accept 值。

    注意：accept 是**值**的允许集合（例如 C04），不是要素名本身。
    自动生成的 accept=[key] 会让"要素名出现在答案里"冒充回答，故字符串声明不生成 accept。
    """
    if isinstance(el, str):
        return {"key": el, "accept": [], "critical": True}
    d = dict(el)
    d.setdefault("key", "")
    d.setdefault("accept", [])
    d.setdefault("critical", True)
    return d


def _value_answers(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False
    low = v.lower()
    if any(m in v or m in low for m in UNANSWERED_MARKERS):
        return False
    if any(m in v or m in low for m in STALE_MARKERS):
        return False
    return True


def element_evidence(state: AgentState, element: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """要素是否被回答，以及支持它的 evidence。同时看 facts 与 materials。"""
    key = element.get("key", "")
    accept = [a for a in (element.get("accept") or []) if a]
    hits: List[str] = []

    def matches(text: str) -> bool:
        if accept:
            return any(a in text for a in accept)
        return _value_answers(text)

    for f in state.facts:
        if key and (f.qualifiers or {}).get("key") != key:
            continue
        if accept:
            if matches(f.text):
                hits.extend(f.evidence_ids)
        elif _value_answers(f.text):
            hits.extend(f.evidence_ids)
    for m in state.materials:
        if m.get("kind") == "summary":
            for k, v in FLAT_LINE_RE.findall(m.get("text", "")):
                if k == key and (matches(v) if accept else _value_answers(v)):
                    hits.extend(m.get("evidence_ids", []))
        else:
            if key and m.get("key") not in (None, "", key):
                continue
            val = m.get("value") or m.get("text", "")
            if matches(val) if accept else _value_answers(val):
                hits.extend(m.get("evidence_ids", []))
    return (bool(hits), hits)


def compute_open(state: AgentState, elements: List[Any]) -> List[Dict[str, Any]]:
    open_items: List[Dict[str, Any]] = []
    for raw in elements:
        el = normalize_element(raw)
        ok, _ = element_evidence(state, el)
        if not ok:
            open_items.append({"goal": el["key"], "why_open": "not_answered"})
    return open_items


# --------------------------------------------------------------------------
# 后端
# --------------------------------------------------------------------------
class StateBackend:
    name = "base"

    def __init__(self, elements: Optional[List[Any]] = None):
        self.elements: List[Dict[str, Any]] = [normalize_element(e) for e in (elements or [])]

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
    ) -> StateUpdate:  # pragma: no cover
        raise NotImplementedError

    def render_for_controller(self, task: Task, state: AgentState) -> str:
        return state.summary or "；".join(state.fact_texts())

    def _line_materials(self, response_text: str, evidence: List[Evidence]) -> List[Dict[str, Any]]:
        """按行生成材料，并关联到包含该行的 evidence。"""
        out: List[Dict[str, Any]] = []
        for line in response_text.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            ev_ids = [e.evidence_id for e in evidence if value and value in e.text]
            if not ev_ids:
                continue
            out.append({"kind": "raw_line", "key": key, "value": value, "text": f"{key}：{value}", "evidence_ids": ev_ids})
        return out

    def _fail(self, state: AgentState, status: Status, event_seq: int, text: str) -> StateUpdate:
        item = {"reason": status.value, "event_seq": event_seq, "excerpt": text[:160], "scope": "episode"}
        state.failed.append(item)
        return StateUpdate(backend=self.name, failed=[item])


class RawHistoryBackend(StateBackend):
    """原文组：不抽事实；材料是原文行，可按路径回查。"""

    name = "raw_history"

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        state.source_event_seq = event_seq
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR, Status.INVALID_OUTPUT):
            return self._fail(state, status, event_seq, response_text)
        materials = self._line_materials(response_text, evidence)
        # 原文组保留整段原文引用（即使没有键值行，也能在装配时被消费）
        if not materials and response_text.strip():
            materials = [
                {
                    "kind": "raw_response",
                    "key": "",
                    "value": response_text.strip()[:2000],
                    "text": response_text.strip()[:2000],
                    "evidence_ids": [e.evidence_id for e in evidence],
                }
            ]
        state.materials.extend(materials)
        return StateUpdate(backend=self.name, materials=materials)


class SummaryBackend(StateBackend):
    """摘要组：受限长度摘要；材料是摘要本身与加工引用。"""

    name = "summary"

    def __init__(self, elements: Optional[List[Any]] = None, max_chars: int = 400):
        super().__init__(elements)
        self.max_chars = max_chars

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        state.source_event_seq = event_seq
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR, Status.INVALID_OUTPUT):
            return self._fail(state, status, event_seq, response_text)
        snippet = " ".join(response_text.split())
        merged = (state.summary + " " + snippet).strip() if state.summary else snippet
        state.summary = merged[-self.max_chars :]
        material = {
            "kind": "summary",
            "key": "",
            "value": state.summary,
            "text": state.summary,
            "evidence_ids": [e.evidence_id for e in evidence],
        }
        state.materials.append(material)
        return StateUpdate(backend=self.name, summary=state.summary, materials=[material], notes=["summary 截断"])


class FoxBackend(StateBackend):
    """F/O/X 组：按键值行抽取命题，保留出处与限定条件。"""

    name = "fox"

    def apply(self, task, state, *, response_text, evidence, status, event_seq, extractor_ref="rule@v1") -> StateUpdate:
        state.source_event_seq = event_seq
        if status in (Status.REFUSED, Status.TIMEOUT, Status.TRANSPORT_ERROR, Status.INVALID_OUTPUT):
            return self._fail(state, status, event_seq, response_text)

        update = StateUpdate(backend=self.name)
        by_text = {e.text: e.evidence_id for e in evidence}
        for line in response_text.splitlines():
            m = LINE_RE.match(line)
            if not m:
                continue
            key, value = m.group(1).strip(), m.group(2).strip()
            if not value:
                continue
            evid = next((eid for text, eid in by_text.items() if value in text), None)
            if evid is None:
                state.hypotheses.append({"text": f"{key}: {value}", "reason": "no_evidence"})
                update.notes.append(f"{key} 无出处，记为 hypothesis")
                continue
            if any(p.text == value and (p.qualifiers or {}).get("key") == key for p in state.facts):
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
            state.facts.append(prop)
            update.new_facts.append({"fact_id": prop.fact_id, "key": key, "text": prop.text, "evidence_ids": prop.evidence_ids})
            state.materials.append({"kind": "fact", "key": key, "value": value, "text": f"{key}：{value}", "evidence_ids": [evid]})
        return update

    def render_for_controller(self, task: Task, state: AgentState) -> str:
        parts = ["已知事实："]
        parts += [f"- {f.qualifiers.get('key', '')}: {f.text}" for f in state.facts] or ["- （空）"]
        parts.append("缺项：" + ("、".join(o["goal"] for o in state.open) if state.open else "无"))
        if state.failed:
            parts.append("失败方向：" + "；".join(str(x.get("reason")) for x in state.failed[:3]))
        return "\n".join(parts)


def make_backend(name: str, elements: Optional[List[Any]] = None, max_chars: int = 400) -> StateBackend:
    key = (name or "raw_history").lower()
    if key in ("raw", "raw_history", "original"):
        return RawHistoryBackend(elements)
    if key == "summary":
        return SummaryBackend(elements, max_chars=max_chars)
    if key in ("fox", "f_o_x", "structured"):
        return FoxBackend(elements)
    raise ValueError(f"未知状态后端：{name}")


# --------------------------------------------------------------------------
# 快照与恢复
# --------------------------------------------------------------------------
def snapshot_state(state: AgentState) -> Dict[str, Any]:
    return asdict(state)


def _state_from_snapshot(snap: Dict[str, Any]) -> AgentState:
    facts = [
        Proposition(**{k: v for k, v in f.items() if k in Proposition.__dataclass_fields__})
        for f in snap.get("facts", [])
    ]
    for f in facts:  # 枚举字段从字符串还原
        f.origin = Origin(f.origin)
        f.fidelity = Fidelity(f.fidelity)
        f.support_status = SupportStatus(f.support_status)
    return AgentState(
        facts=facts,
        summary=snap.get("summary", ""),
        open=list(snap.get("open", [])),
        failed=list(snap.get("failed", [])),
        hypotheses=list(snap.get("hypotheses", [])),
        materials=list(snap.get("materials", [])),
        budget_left=dict(snap.get("budget_left", {})),
        source_event_seq=int(snap.get("source_event_seq", 0)),
        node_id=snap.get("node_id", ""),
        parent_state_seq=int(snap.get("parent_state_seq", 0)),
        extractor_version=snap.get("extractor_version", ""),
    )


def restore_state(store, backend: StateBackend, task: Task, node_id: Optional[str] = None) -> AgentState:
    """按最近一次 state_write 快照恢复指定节点；不调用模型、不合并兄弟分支。"""
    target = node_id
    chosen: Optional[Dict[str, Any]] = None
    for ev in store.of_type("state_write"):
        payload = ev.payload or {}
        snap = payload.get("snapshot")
        if not snap:
            continue
        if target is not None and payload.get("node") != target:
            continue
        chosen = snap
    if chosen is None:
        return AgentState()
    return _state_from_snapshot(chosen)
