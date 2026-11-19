"""预算守卫与结算（手册 §8.2、§8.3）。

规则：
- 每次模型调用前必须 reserve；未持票调用视为实现错误；
- 已发出的请求不退款（拒答、超时、后来丢弃的分支都计入）；
- 未发出即可 release（预留释放）；
- 结算按 call_id 唯一，避免同一调用重复计账；
- 恢复、评分、装配、选择、分析都不能绕过预算。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .contracts import BudgetLimits, new_id
from .events import EventStore


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class Ticket:
    ticket_id: str
    kind: str
    dispatched: bool = False
    settled: bool = False
    estimate: int = 1
    stage_id: str = ""


class BudgetGuard:
    """reserve → dispatch → settle。计数器只由结算增长，预留单独计。"""

    def __init__(self, limits: BudgetLimits, store: EventStore, episode_id: str):
        self.limits = limits
        self.store = store
        self.episode_id = episode_id
        self._tickets: Dict[str, Ticket] = {}
        self._used = {"target": 0, "non_target": 0}
        self._reserved = {"target": 0, "non_target": 0}
        self._settled_calls: set[str] = set()
        self._tokens = 0
        self._wall_ms = 0
        # 阶段限额：只统计被配置约束的阶段，未配置阶段只受总额约束
        self._stage_caps: Dict[str, int] = {str(k): int(v) for k, v in (limits.stage_calls or {}).items()}
        self._stage_used: Dict[str, int] = {k: 0 for k in self._stage_caps}
        self._stage_reserved: Dict[str, int] = {k: 0 for k in self._stage_caps}

    # ---- 分类 ----
    @staticmethod
    def _bucket(kind: str) -> str:
        return "target" if kind == "target" else "non_target"

    def _limit(self, bucket: str) -> Optional[int]:
        return self.limits.target_calls if bucket == "target" else self.limits.non_target_calls

    # ---- 生命周期 ----
    def reserve(self, kind: str, estimate: int = 1, stage_id: str = "") -> str:
        bucket = self._bucket(kind)
        limit = self._limit(bucket)
        if limit is not None and self._used[bucket] + self._reserved[bucket] + estimate > limit:
            raise BudgetExhausted(f"{bucket} 预算不足：used={self._used[bucket]} reserved={self._reserved[bucket]} limit={limit}")
        stage = str(stage_id or "")
        if stage in self._stage_caps:
            cap = self._stage_caps[stage]
            if self._stage_used[stage] + self._stage_reserved[stage] + estimate > cap:
                raise BudgetExhausted(
                    f"阶段预算不足：stage={stage} used={self._stage_used[stage]} "
                    f"reserved={self._stage_reserved[stage]} limit={cap}"
                )
        ticket = Ticket(ticket_id=new_id("tk"), kind=kind, estimate=estimate, stage_id=stage)
        self._tickets[ticket.ticket_id] = ticket
        self._reserved[bucket] += estimate
        if stage in self._stage_reserved:
            self._stage_reserved[stage] += estimate
        self.store.append(
            "budget_reserved",
            role=kind,
            stage_id=stage,
            payload={"ticket": ticket.ticket_id, "kind": kind, "estimate": estimate, "stage_id": stage},
            budget_after=self.snapshot(),
        )
        return ticket.ticket_id

    def dispatch(self, ticket_id: str) -> None:
        t = self._tickets[ticket_id]
        if t.settled:
            raise RuntimeError(f"票据已结算：{ticket_id}")
        t.dispatched = True

    def release(self, ticket_id: str) -> None:
        """未发出的预留可释放；已发出的不允许退款。"""
        t = self._tickets.get(ticket_id)
        if t is None or t.settled:
            return
        if t.dispatched:
            raise RuntimeError("已发出的请求不退款")
        bucket = self._bucket(t.kind)
        self._reserved[bucket] = max(0, self._reserved[bucket] - t.estimate)
        if t.stage_id in self._stage_reserved:
            self._stage_reserved[t.stage_id] = max(0, self._stage_reserved[t.stage_id] - t.estimate)
        t.settled = True
        self.store.append(
            "budget_settled",
            role=t.kind,
            payload={"ticket": ticket_id, "released": True},
            cost={},
            budget_after=self.snapshot(),
        )

    def settle(self, ticket_id: str, cost: Dict[str, Any], call_id: str = "") -> Dict[str, Any]:
        t = self._tickets[ticket_id]
        if t.settled:
            raise RuntimeError(f"票据重复结算：{ticket_id}")
        if call_id and call_id in self._settled_calls:
            raise RuntimeError(f"调用重复结算：{call_id}")
        bucket = self._bucket(t.kind)
        self._reserved[bucket] = max(0, self._reserved[bucket] - t.estimate)
        if t.stage_id in self._stage_reserved:
            self._stage_reserved[t.stage_id] = max(0, self._stage_reserved[t.stage_id] - t.estimate)
            self._stage_used[t.stage_id] += 1
        self._used[bucket] += 1
        if call_id:
            self._settled_calls.add(call_id)
        self._tokens += int(cost.get("tokens_in") or 0) + int(cost.get("tokens_out") or 0)
        self._wall_ms += int(cost.get("wall_ms") or 0)
        t.settled = True
        snap = self.snapshot()
        self.store.append(
            "budget_settled",
            role=t.kind,
            stage_id=t.stage_id,
            call_id=call_id,
            payload={"ticket": ticket_id, "stage_id": t.stage_id},
            cost=cost,
            budget_after=snap,
        )
        return snap

    # ---- 观测 ----
    def snapshot(self) -> Dict[str, Any]:
        return {
            "target_calls_used": self._used["target"],
            "target_calls_left": (self.limits.target_calls - self._used["target"]) if self.limits.target_calls is not None else None,
            "non_target_calls_used": self._used["non_target"],
            "non_target_calls_left": (
                self.limits.non_target_calls - self._used["non_target"] if self.limits.non_target_calls is not None else None
            ),
            "non_target_reserved": self._reserved["non_target"],
            "tokens_used": self._tokens,
            "wall_ms": self._wall_ms,
            "stage_calls_used": {k: self._stage_used[k] for k in sorted(self._stage_used)},
            "stage_calls_left": {
                k: max(0, self._stage_caps[k] - self._stage_used[k] - self._stage_reserved[k]) for k in sorted(self._stage_caps)
            },
        }

    def stage_left(self, stage_id: str) -> Optional[int]:
        """阶段剩余额度；未配置该阶段返回 None（只受总额约束）。"""
        stage = str(stage_id or "")
        if stage not in self._stage_caps:
            return None
        return max(0, self._stage_caps[stage] - self._stage_used[stage] - self._stage_reserved[stage])

    def can_spend_target(self) -> bool:
        limit = self.limits.target_calls
        return limit is None or self._used["target"] + self._reserved["target"] < limit

    def target_calls_used(self) -> int:
        return self._used["target"]
