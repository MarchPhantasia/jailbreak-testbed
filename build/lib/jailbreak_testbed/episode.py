"""Episode 编排：把日志、预算与视图串成一次可回放的攻击尝试。

M0 阶段只提供骨架：没有真实模型调用，只有"记录与结算"。
方法层（Frontier/Scorer/Controller/Assembler）在 M2 之后接入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .budget import Budget, BudgetExhausted, BudgetGuard, Ticket
from .events import Cost, Event, EventLog, EventType, ZERO
from .state import AgentState, FailedItem, Fact, HistoryView, OpenItem

SESSION_MODES = ("stateless", "server_side")


@dataclass
class SendHandle:
    """一次目标请求的句柄：既带票据（用于结算），也带事件序号（用于回退与出处）。"""

    ticket: Ticket
    seq: int          # view_snapshot 的 seq：目标"看到"这一轮的起点
    prompt_seq: int
    view_id: int


@dataclass
class Episode:
    episode_id: str
    log: EventLog
    budget: Budget
    seed: int = 0
    session_mode: str = "stateless"
    config_hash: str = ""

    guard: BudgetGuard = field(init=False)
    state: AgentState = field(default_factory=AgentState)
    view: HistoryView = field(default_factory=HistoryView)
    terminated: bool = False
    _tickets: List[Ticket] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.session_mode not in SESSION_MODES:
            raise ValueError(f"session_mode 必须是 {SESSION_MODES}")
        self.guard = BudgetGuard(self.budget)

    # ---------- 基础 ----------
    @classmethod
    def open(cls, directory: str, **kwargs: Any) -> "Episode":
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "events.jsonl")
        return cls(log=EventLog(path), **kwargs)

    def _emit(self, type_: EventType, payload: Dict[str, Any], cost: Cost = ZERO,
              served: bool = True, tags: Optional[Dict[str, Any]] = None) -> Event:
        if self.terminated and type_ != EventType.TERMINATE:
            raise RuntimeError("episode 已终止，不能再写事件")
        return self.log.append(
            type_,
            payload=payload,
            cost=cost,
            served=served,
            budget_after=self.guard.remaining(),
            tags=tags or {"seed": self.seed, "session_mode": self.session_mode},
        )

    # ---------- 生命周期 ----------
    def plan(self, action: Dict[str, Any]) -> Event:
        return self._emit(EventType.PLAN, {"action": action})

    def send(self, prompt: str, meta: Optional[Dict[str, Any]] = None) -> SendHandle:
        """发出一次目标请求：先预留预算，再写请求与视图快照。"""
        ticket = self.guard.reserve("target_calls", 1)
        self._tickets.append(ticket)
        snap = self._emit(
            EventType.VIEW_SNAPSHOT,
            {"view": self.view.as_dict(), "prompt": prompt, "meta": meta or {}},
        )
        self.view.append(snap.seq)
        sent = self._emit(
            EventType.PROMPT_SENT,
            {"prompt": prompt, "view_id": self.view.view_id, "ticket": ticket.estimate},
        )
        return SendHandle(ticket=ticket, seq=snap.seq, prompt_seq=sent.seq, view_id=self.view.view_id)

    def observe(
        self,
        ticket: Optional[Any],
        response: Optional[str] = None,
        blocked: bool = False,
        tokens_out: int = 0,
        wall_ms: int = 0,
        served: bool = True,
    ) -> Event:
        """结算并记录观测。blocked=True 时写 defense_blocked。"""
        if isinstance(ticket, SendHandle):
            ticket = ticket.ticket
        cost = Cost(tokens_out=tokens_out, wall_ms=wall_ms)
        if ticket is not None:
            settled = self.guard.settle(ticket, 1)
            cost = cost + settled
        elif served:
            raise RuntimeError("没有预算票据却发起真实调用（实现错误）")
        if blocked:
            return self._emit(EventType.DEFENSE_BLOCKED, {"response": response}, cost=cost, served=served)
        return self._emit(EventType.RESPONSE, {"response": response or ""}, cost=cost, served=served)

    def cache_hit(self, prompt: str, response: str) -> Event:
        """缓存命中：不产生 served 调用。"""
        return self._emit(
            EventType.CACHE_HIT, {"prompt": prompt, "response": response}, cost=ZERO, served=False
        )

    def write_state(
        self,
        facts: Optional[List[Fact]] = None,
        open_items: Optional[List[OpenItem]] = None,
        failed: Optional[FailedItem] = None,
    ) -> Event:
        added = []
        for f in facts or []:
            if self.state.add_fact(f):
                added.append(f.text)
        for o in open_items or []:
            self.state.add_open(o)
        if failed is not None:
            self.state.add_failed(failed)
        return self._emit(
            EventType.STATE_WRITE,
            {
                "facts": [
                    {"text": f.text, "src_events": list(f.src_events),
                     "confidence": f.confidence, "fidelity": f.fidelity}
                    for f in (facts or [])
                ],
                "open": [{"goal": o.goal, "why_open": o.why_open} for o in (open_items or [])],
                "failed": (
                    [{"attempt": failed.attempt, "reason_class": failed.reason_class,
                      "src_events": list(failed.src_events)}]
                    if failed else []
                ),
                "extractor_version": self.state.extractor_version,
                "added": added,
            },
        )

    def rollback(self, drop_from_seq: int) -> Event:
        before = self.view.view_id
        self.view = self.view.rollback(drop_from_seq)
        return self._emit(
            EventType.ROLLBACK,
            {"from_view": before, "to_view": self.view.view_id, "drop_from_seq": drop_from_seq},
        )

    def assemble(self, k: int, picked: str, scores: Optional[List[float]] = None,
                 tokens_out: int = 0) -> Event:
        return self._emit(
            EventType.ASSEMBLED,
            {"k": k, "picked": picked, "scores": scores or []},
            cost=Cost(tokens_out=tokens_out),
        )

    def judge(self, phase: str, cls: str, judge_id: str, coverage: Optional[float] = None,
              tokens_out: int = 0, served: bool = True) -> Event:
        cost = Cost(tokens_out=tokens_out)
        if phase == "online":
            t = self.guard.reserve("judge_calls", 1)
            self._tickets.append(t)
            cost = cost + self.guard.settle(t, 1)
        elif phase != "offline":
            raise ValueError("phase 必须是 online 或 offline")
        return self._emit(
            EventType.JUDGE,
            {"phase": phase, "class": cls, "judge_id": judge_id, "coverage": coverage},
            cost=cost,
            served=served,
        )

    def terminate(self, reason: str, success: bool = False) -> Event:
        if self.terminated:
            raise RuntimeError("episode 已终止")
        self.guard.assert_settled(self._tickets)
        self.terminated = True
        return self._emit(EventType.TERMINATE, {"reason": reason, "success": success})
