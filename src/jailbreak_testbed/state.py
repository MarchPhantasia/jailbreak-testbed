"""状态与视图。

两条不变量（docs/spec/02-contracts.md C3）：
1. 事实必须有出处：src_events 非空。
2. fidelity 必填：出处只证明"引用了哪里"，不证明"抽取是否正确"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

VALID_FIDELITY = ("verbatim", "paraphrase", "inferred")
VALID_REASON = ("refusal", "vague", "offtopic", "circular", "premature")


class StateError(ValueError):
    """状态不变量被违反。"""


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


@dataclass
class Fact:
    text: str
    src_events: List[int]
    confidence: float = 0.7
    fidelity: str = "verbatim"

    def __post_init__(self) -> None:
        if not self.src_events:
            raise StateError("事实必须带 src_events（没有出处的事实不允许进状态）")
        if self.fidelity not in VALID_FIDELITY:
            raise StateError(f"非法 fidelity: {self.fidelity}，应为 {VALID_FIDELITY}")
        if not (0.0 <= float(self.confidence) <= 1.0):
            raise StateError(f"confidence 越界: {self.confidence}")

    def key(self) -> str:
        return _norm(self.text)


@dataclass
class OpenItem:
    goal: str
    why_open: str = ""


@dataclass
class FailedItem:
    attempt: str
    reason_class: str
    src_events: List[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.reason_class not in VALID_REASON:
            raise StateError(f"非法 reason_class: {self.reason_class}，应为 {VALID_REASON}")


@dataclass
class AgentState:
    facts: Dict[str, Fact] = field(default_factory=dict)   # key = 规范化文本
    open: List[OpenItem] = field(default_factory=list)
    failed: List[FailedItem] = field(default_factory=list)
    extractor_version: str = "x1"

    # ---- 变更 ----
    def add_fact(self, fact: Fact, replace: bool = False) -> bool:
        """加入事实。返回是否真的新增（重复时默认不覆盖）。"""
        k = fact.key()
        if k in self.facts and not replace:
            return False
        self.facts[k] = fact
        return True

    def add_failed(self, item: FailedItem) -> None:
        self.failed.append(item)

    def add_open(self, item: OpenItem) -> None:
        self.open.append(item)

    # ---- 观测 ----
    def inferred_ratio(self) -> float:
        if not self.facts:
            return 0.0
        inferred = sum(1 for f in self.facts.values() if f.fidelity == "inferred")
        return inferred / len(self.facts)

    def fidelity_warning(self, threshold: float = 0.2) -> bool:
        return self.inferred_ratio() > threshold

    def fact_texts(self) -> List[str]:
        return [f.text for f in self.facts.values()]

    # ---- 序列化（state_write 的 payload）----
    def to_payload(self) -> Dict[str, Any]:
        return {
            "facts": [
                {
                    "text": f.text,
                    "src_events": list(f.src_events),
                    "confidence": f.confidence,
                    "fidelity": f.fidelity,
                }
                for f in self.facts.values()
            ],
            "open": [{"goal": o.goal, "why_open": o.why_open} for o in self.open],
            "failed": [
                {"attempt": x.attempt, "reason_class": x.reason_class, "src_events": list(x.src_events)}
                for x in self.failed
            ],
            "extractor_version": self.extractor_version,
        }

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "AgentState":
        st = cls(extractor_version=str(payload.get("extractor_version", "x1")))
        for f in payload.get("facts") or []:
            st.add_fact(
                Fact(
                    text=f["text"],
                    src_events=list(f.get("src_events") or []),
                    confidence=float(f.get("confidence", 0.7)),
                    fidelity=f.get("fidelity", "verbatim"),
                )
            )
        for o in payload.get("open") or []:
            st.add_open(OpenItem(goal=o.get("goal", ""), why_open=o.get("why_open", "")))
        for x in payload.get("failed") or []:
            st.add_failed(
                FailedItem(
                    attempt=x.get("attempt", ""),
                    reason_class=x.get("reason_class", "vague"),
                    src_events=list(x.get("src_events") or []),
                )
            )
        return st


@dataclass
class HistoryView:
    """目标此刻能看到的事件集合。回退只改这里，不改事件流。"""

    view_id: int = 1
    visible_seqs: List[int] = field(default_factory=list)

    def append(self, seq: int) -> None:
        self.visible_seqs.append(seq)

    def rollback(self, drop_from_seq: int) -> "HistoryView":
        """回退到某条事件之前：可见集合中去掉 >= drop_from_seq 的事件，版本 +1。"""
        if not self.visible_seqs:
            return HistoryView(view_id=self.view_id + 1, visible_seqs=[])
        kept = [s for s in self.visible_seqs if s < drop_from_seq]
        return HistoryView(view_id=self.view_id + 1, visible_seqs=kept)

    def as_dict(self) -> Dict[str, Any]:
        return {"view_id": self.view_id, "visible_seqs": list(self.visible_seqs)}


def apply_state_writes(events: Iterable[Any]) -> AgentState:
    """restore 模式：按事件顺序重放 state_write，得到"当时的状态"。"""
    state = AgentState()
    for e in events:
        if getattr(e, "type", None) is None or e.type.value != "state_write":
            continue
        payload = e.payload or {}
        state.extractor_version = str(payload.get("extractor_version", state.extractor_version))
        for f in payload.get("facts") or []:
            state.add_fact(
                Fact(
                    text=f["text"],
                    src_events=list(f.get("src_events") or []),
                    confidence=float(f.get("confidence", 0.7)),
                    fidelity=f.get("fidelity", "verbatim"),
                )
            )
        for o in payload.get("open") or []:
            state.add_open(OpenItem(goal=o.get("goal", ""), why_open=o.get("why_open", "")))
        for x in payload.get("failed") or []:
            state.add_failed(
                FailedItem(
                    attempt=x.get("attempt", ""),
                    reason_class=x.get("reason_class", "vague"),
                    src_events=list(x.get("src_events") or []),
                )
            )
    return state
