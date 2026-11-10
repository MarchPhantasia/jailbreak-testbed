"""Append-only event log — the single source of truth.

一切状态、指标、判定都是事件流的投影。回退不删除事件，只抬高视图版本。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, Iterable, Iterator, List, Optional


class EventType(str, Enum):
    PLAN = "plan"
    PROMPT_SENT = "prompt_sent"
    VIEW_SNAPSHOT = "view_snapshot"
    RESPONSE = "response"
    DEFENSE_BLOCKED = "defense_blocked"
    STATE_WRITE = "state_write"
    ASSEMBLED = "assembled"
    JUDGE = "judge"
    DECIDE = "decide"
    ROLLBACK = "rollback"
    CACHE_HIT = "cache_hit"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class Cost:
    """单次事件消耗。analyze 阶段的花费不进这里（见 docs/spec/03-runtime.md）。"""

    target_calls: int = 0
    judge_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    wall_ms: int = 0

    def __add__(self, other: "Cost") -> "Cost":
        return Cost(
            self.target_calls + other.target_calls,
            self.judge_calls + other.judge_calls,
            self.tokens_in + other.tokens_in,
            self.tokens_out + other.tokens_out,
            self.wall_ms + other.wall_ms,
        )

    def as_dict(self) -> Dict[str, int]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "Cost":
        d = d or {}
        return cls(
            target_calls=int(d.get("target_calls", 0)),
            judge_calls=int(d.get("judge_calls", 0)),
            tokens_in=int(d.get("tokens_in", 0)),
            tokens_out=int(d.get("tokens_out", 0)),
            wall_ms=int(d.get("wall_ms", 0)),
        )


ZERO = Cost()


@dataclass
class Event:
    seq: int
    type: EventType
    payload: Dict[str, Any] = field(default_factory=dict)
    cost: Cost = ZERO
    served: bool = True
    budget_after: Dict[str, int] = field(default_factory=dict)
    tags: Dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "type": self.type.value,
            "payload": self.payload,
            "cost": self.cost.as_dict(),
            "served": self.served,
            "budget_after": self.budget_after,
            "tags": self.tags,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Event":
        return cls(
            seq=int(d["seq"]),
            type=EventType(d["type"]),
            payload=d.get("payload") or {},
            cost=Cost.from_dict(d.get("cost")),
            served=bool(d.get("served", True)),
            budget_after=d.get("budget_after") or {},
            tags=d.get("tags") or {},
            ts=d.get("ts", ""),
        )


class EventLog:
    """JSONL 追加写入器。seq 由日志分配，保证单调。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._last_seq = self._read_last_seq()

    def _read_last_seq(self) -> int:
        if not os.path.exists(self.path):
            return 0
        last = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = int(json.loads(line)["seq"])
                except (json.JSONDecodeError, KeyError):
                    continue
        return last

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def append(
        self,
        type: EventType,
        payload: Optional[Dict[str, Any]] = None,
        cost: Cost = ZERO,
        served: bool = True,
        budget_after: Optional[Dict[str, int]] = None,
        tags: Optional[Dict[str, Any]] = None,
    ) -> Event:
        if isinstance(type, str):
            type = EventType(type)
        ev = Event(
            seq=self._last_seq + 1,
            type=type,
            payload=payload or {},
            cost=cost,
            served=served,
            budget_after=budget_after or {},
            tags=tags or {},
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
        self._last_seq = ev.seq
        return ev


def read_events(path: str) -> List[Event]:
    if not os.path.exists(path):
        return []
    out: List[Event] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(Event.from_dict(json.loads(line)))
    return out


def iter_by_type(events: Iterable[Event], type_: EventType) -> Iterator[Event]:
    for e in events:
        if e.type == type_:
            yield e
