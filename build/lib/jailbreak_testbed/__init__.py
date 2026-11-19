"""Jailbreak Testbed — replayable, budget-accounted experiment infrastructure.

当前阶段：M0（日志 + 账本 + 回放）。方法层尚未实现。
"""
from .budget import Budget, BudgetExhausted, BudgetGuard, Ledger, Ticket, TicketError
from .episode import Episode
from .events import Event, EventLog, EventType, Cost, read_events
from .replay import ReplayResult, compute_metrics, replay
from .state import AgentState, FailedItem, Fact, HistoryView, OpenItem, StateError

__version__ = "0.1.0"

__all__ = [
    "Budget", "BudgetExhausted", "BudgetGuard", "Ledger", "Ticket", "TicketError",
    "Episode", "Event", "EventLog", "EventType", "Cost", "read_events",
    "ReplayResult", "compute_metrics", "replay",
    "AgentState", "FailedItem", "Fact", "HistoryView", "OpenItem", "StateError",
]
