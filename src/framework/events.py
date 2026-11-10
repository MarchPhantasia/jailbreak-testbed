"""事件存储与账本派生（手册 §13）。

事件是运行事实的**主记录**；账本与指标都由事件派生，不作为第二份可改写的真相。
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .contracts import Phase, Role, new_id

#: 手册 §13 列出的全部事件类型（顺序即语义顺序）
EVENT_TYPES = (
    "episode_started",
    "action_proposed",
    "action_rejected",
    "budget_reserved",
    "request_sent",
    "response_received",
    "budget_settled",
    "view_snapshot",
    "state_write",
    "node_created",
    "frontier_updated",
    "node_selected",
    "score_recorded",
    "assembled",
    "candidate_selected",
    "online_verdict",
    "target_refusal",
    "defense_blocked",
    "rollback",
    "component_refusal",
    "component_error",
    "recovery_decision",
    "cache_hit",
    "terminate",
)


@dataclass
class Event:
    event_id: str
    episode_id: str
    seq: int
    ts: float
    type: str
    phase: str = Phase.ONLINE.value
    role: str = ""
    stage_id: str = ""
    call_id: str = ""
    attempt_id: str = ""
    node_id: str = ""
    view_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    budget_after: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Event":
        known = {f for f in Event.__dataclass_fields__}  # type: ignore[attr-defined]
        return Event(**{k: v for k, v in d.items() if k in known})


class EventStore:
    """append-only JSONL 事件存储，每 episode 一个文件。"""

    def __init__(self, root: str, episode_id: str):
        self.root = root
        self.episode_id = episode_id
        self.path = os.path.join(root, "episodes", episode_id, "events.jsonl")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._seq = 0
        self._events: List[Event] = []
        if os.path.exists(self.path):
            for line in open(self.path, "r", encoding="utf-8"):
                line = line.strip()
                if line:
                    ev = Event.from_dict(json.loads(line))
                    self._events.append(ev)
                    self._seq = max(self._seq, ev.seq)

    # ---- 写 ----
    def append(
        self,
        type: str,
        *,
        phase: Phase = Phase.ONLINE,
        role: Role | str = "",
        stage_id: str = "",
        call_id: str = "",
        attempt_id: str = "",
        node_id: str = "",
        view_id: str = "",
        payload: Optional[Dict[str, Any]] = None,
        cost: Optional[Dict[str, Any]] = None,
        budget_after: Optional[Dict[str, Any]] = None,
    ) -> Event:
        if type not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{type}")
        self._seq += 1
        ev = Event(
            event_id=new_id("evt"),
            episode_id=self.episode_id,
            seq=self._seq,
            ts=time.time(),
            type=type,
            phase=phase.value if isinstance(phase, Phase) else str(phase),
            role=role.value if isinstance(role, Role) else str(role),
            stage_id=stage_id,
            call_id=call_id,
            attempt_id=attempt_id,
            node_id=node_id,
            view_id=view_id,
            payload=payload or {},
            cost=cost or {},
            budget_after=budget_after or {},
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        self._events.append(ev)
        return ev

    # ---- 读 ----
    @property
    def events(self) -> List[Event]:
        return list(self._events)

    def of_type(self, *types: str) -> List[Event]:
        want = set(types)
        return [e for e in self._events if e.type in want]

    def last(self, type: str) -> Optional[Event]:
        for e in reversed(self._events):
            if e.type == type:
                return e
        return None

    def count(self, type: str) -> int:
        return sum(1 for e in self._events if e.type == type)

    # ---- 派生 ----
    def ledger(self) -> Dict[str, Any]:
        """由事件聚合出账本；未知用量单独计数，不写成实测 0（R12）。"""
        calls: Counter = Counter()
        served = 0
        cached = 0
        tokens_in = tokens_out = 0
        unknown_in = unknown_out = unknown_any = 0
        settled_calls = 0
        wall_ms = 0
        for e in self._events:
            if e.type == "request_sent":
                calls[f"{e.role}"] += 1
                if e.payload.get("served", True):
                    served += 1
            if e.type == "cache_hit":
                cached += 1
            if e.type == "budget_settled" and not e.payload.get("released"):
                settled_calls += 1
                c = e.cost or {}
                in_known = c.get("tokens_in") is not None
                out_known = c.get("tokens_out") is not None
                if not in_known:
                    unknown_in += 1
                else:
                    tokens_in += int(c["tokens_in"])
                if not out_known:
                    unknown_out += 1
                else:
                    tokens_out += int(c["tokens_out"])
                if not (in_known and out_known):
                    unknown_any += 1
                wall_ms += int(c.get("wall_ms") or 0)
        return {
            "calls": dict(calls),
            "target_calls": calls.get(Role.TARGET.value, 0),
            "served_calls": served,
            "cache_hits": cached,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_in_unknown_calls": unknown_in,
            "tokens_out_unknown_calls": unknown_out,
            "usage_unknown_calls": unknown_any,
            "settled_calls": settled_calls,
            # 完整覆盖要求输入与输出都已知；任一侧缺失都不算满额
            "usage_coverage": (settled_calls - unknown_any) / settled_calls if settled_calls else None,
            "usage_coverage_in": (settled_calls - unknown_in) / settled_calls if settled_calls else None,
            "usage_coverage_out": (settled_calls - unknown_out) / settled_calls if settled_calls else None,
            "wall_ms": wall_ms,
        }

    def terminations(self) -> List[Event]:
        return self.of_type("terminate")
