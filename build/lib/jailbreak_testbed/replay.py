"""回放：从事件流重建状态与指标。

两种模式（docs/adr/0004-event-log-and-view.md）：
- restore  ：按 state_write 事件恢复"当时写入了什么"
- recompute：用（可能已更新的）抽取器对 response 事件重算状态
两者结果不同，本身就是需要报告的事实（抽取器版本漂移）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from .budget import Ledger
from .events import Event, EventType, read_events
from .state import AgentState, apply_state_writes

Extractor = Callable[[List[Event]], Dict[str, Any]]


@dataclass
class ReplayResult:
    mode: str
    state: AgentState
    metrics: Dict[str, Any]
    diff: Dict[str, List[str]] = field(default_factory=dict)

    def summary(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "facts": len(self.state.facts),
            "extractor_version": self.state.extractor_version,
            "inferred_ratio": round(self.state.inferred_ratio(), 4),
            "metrics": self.metrics,
            "diff": self.diff,
        }


def compute_metrics(events: Iterable[Event]) -> Dict[str, Any]:
    events = list(events)
    ledger = Ledger.from_events(events)
    terminate = [e for e in events if e.type == EventType.TERMINATE]
    judge = [e for e in events if e.type == EventType.JUDGE]
    online = [e for e in judge if (e.payload or {}).get("phase") == "online"]
    offline = [e for e in judge if (e.payload or {}).get("phase") == "offline"]
    responses = [e for e in events if e.type == EventType.RESPONSE]
    blocked = [e for e in events if e.type == EventType.DEFENSE_BLOCKED]
    return {
        "n_events": len(events),
        "n_prompt_sent": sum(1 for e in events if e.type == EventType.PROMPT_SENT),
        "n_response": len(responses),
        "n_blocked": len(blocked),
        "n_judge_online": len(online),
        "n_judge_offline": len(offline),
        "n_rollback": sum(1 for e in events if e.type == EventType.ROLLBACK),
        "n_cache_hit": sum(1 for e in events if e.type == EventType.CACHE_HIT),
        "terminate_reason": (terminate[-1].payload or {}).get("reason") if terminate else None,
        "class": (offline[-1].payload or {}).get("class") if offline else None,
        "cost": ledger.cost.as_dict(),
        "logical_calls": ledger.logical_calls,
        "served_calls": ledger.served_calls,
        "by_type": ledger.by_type,
    }


def replay(
    path_or_events: Any,
    mode: str = "restore",
    extractor: Optional[Extractor] = None,
    extractor_version: Optional[str] = None,
) -> ReplayResult:
    if isinstance(path_or_events, str):
        path = (
            os.path.join(path_or_events, "events.jsonl")
            if os.path.isdir(path_or_events)
            else path_or_events
        )
        events: List[Event] = read_events(path)
    else:
        events = list(path_or_events)
    metrics = compute_metrics(events)

    if mode == "restore":
        state = apply_state_writes(events)
        return ReplayResult(mode=mode, state=state, metrics=metrics)

    if mode != "recompute":
        raise ValueError(f"未知模式: {mode}")
    if extractor is None:
        raise ValueError("recompute 模式需要 extractor")

    responses = [e for e in events if e.type == EventType.RESPONSE]
    payload = extractor(responses) or {}
    if extractor_version:
        payload = dict(payload, extractor_version=extractor_version)
    state = AgentState.from_payload(payload)

    restored = apply_state_writes(events)
    old = {f.key() for f in restored.facts.values()}
    new = {f.key() for f in state.facts.values()}
    diff = {
        "added": sorted(new - old),
        "removed": sorted(old - new),
    }
    return ReplayResult(mode=mode, state=state, metrics=metrics, diff=diff)
