"""预算守卫与账本。

规则（docs/spec/03-runtime.md）：
- 所有模型调用必须持票；未持票调用视为实现错误。
- 两类成本分账：runtime 计入预算，analysis 不计入但必须报。
- logical 与 served 分开计数：缓存命中只增加 logical。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .events import Cost, Event, EventType

KINDS = ("target_calls", "judge_calls", "tokens", "wall_ms")


class BudgetExhausted(RuntimeError):
    """预留失败：剩余额度不足。"""


class TicketError(RuntimeError):
    """票据使用错误：未持票结算、重复结算、或结算超过预留。"""


@dataclass
class Budget:
    target_calls: int = 20
    judge_calls: int = 6
    tokens: int = 200_000
    wall_ms: int = 600_000

    def as_dict(self) -> Dict[str, int]:
        return {
            "target_calls": self.target_calls,
            "judge_calls": self.judge_calls,
            "tokens": self.tokens,
            "wall_ms": self.wall_ms,
        }


@dataclass
class Ticket:
    kind: str
    estimate: int
    seq_issued: int = 0
    settled: bool = False


@dataclass
class BudgetGuard:
    """预留 → 结算。

    只做"上限"控制：reserve 检查剩余是否够，settle 扣减实际值。
    不做货币换算：tokens 与 wall_ms 各自独立计数。
    """

    budget: Budget
    spent: Dict[str, int] = field(default_factory=dict)
    reserved: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for k in KINDS:
            self.spent.setdefault(k, 0)
            self.reserved.setdefault(k, 0)

    def remaining(self) -> Dict[str, int]:
        return {k: self.budget.as_dict()[k] - self.spent[k] - self.reserved[k] for k in KINDS}

    def reserve(self, kind: str, estimate: int = 1) -> Ticket:
        if kind not in KINDS:
            raise TicketError(f"未知预算类型: {kind}")
        if estimate < 0:
            raise TicketError("预留量不能为负")
        if self.remaining()[kind] < estimate:
            raise BudgetExhausted(f"{kind} 剩余 {self.remaining()[kind]} < 需要 {estimate}")
        self.reserved[kind] += estimate
        return Ticket(kind=kind, estimate=estimate)

    def settle(self, ticket: Ticket, actual: Optional[int] = None) -> Cost:
        if ticket.settled:
            raise TicketError("票据已结算")
        if ticket.kind not in KINDS:
            raise TicketError(f"未知预算类型: {ticket.kind}")
        used = ticket.estimate if actual is None else int(actual)
        if used < 0:
            raise TicketError("实际消耗不能为负")
        self.reserved[ticket.kind] -= ticket.estimate
        self.spent[ticket.kind] += used
        ticket.settled = True
        if ticket.kind == "target_calls":
            return Cost(target_calls=used)
        if ticket.kind == "judge_calls":
            return Cost(judge_calls=used)
        return Cost()

    def assert_settled(self, tickets: Iterable[Ticket]) -> None:
        open_ = [t for t in tickets if not t.settled]
        if open_:
            raise TicketError(f"存在未结算票据: {[(t.kind, t.estimate) for t in open_]}")


@dataclass
class Ledger:
    """由事件流聚合而来，不单独维护（避免两份真相）。"""

    by_type: Dict[str, int] = field(default_factory=dict)
    cost: Cost = Cost()
    logical_calls: int = 0
    served_calls: int = 0

    @classmethod
    def from_events(cls, events: Iterable[Event]) -> "Ledger":
        ledger = cls()
        for e in events:
            ledger.by_type[e.type.value] = ledger.by_type.get(e.type.value, 0) + 1
            ledger.cost = ledger.cost + e.cost
            if e.cost.target_calls or e.cost.judge_calls:
                ledger.logical_calls += 1
                if e.served:
                    ledger.served_calls += 1
        return ledger

    def as_dict(self) -> Dict[str, object]:
        return {
            "by_type": self.by_type,
            "cost": self.cost.as_dict(),
            "logical_calls": self.logical_calls,
            "served_calls": self.served_calls,
        }


def cost_from_parts(
    target_calls: int = 0,
    judge_calls: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    wall_ms: int = 0,
) -> Cost:
    return Cost(target_calls, judge_calls, tokens_in, tokens_out, wall_ms)


def spent_from_events(events: List[Event]) -> Dict[str, int]:
    c = Ledger.from_events(events).cost
    return {
        "target_calls": c.target_calls,
        "judge_calls": c.judge_calls,
        "tokens": c.tokens_in + c.tokens_out,
        "wall_ms": c.wall_ms,
    }


__all__ = [
    "KINDS",
    "Budget",
    "BudgetGuard",
    "BudgetExhausted",
    "Ticket",
    "TicketError",
    "Ledger",
    "cost_from_parts",
    "spent_from_events",
    "EventType",
]
