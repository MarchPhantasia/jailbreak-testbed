import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jailbreak_testbed.budget import (  # noqa: E402
    Budget,
    BudgetExhausted,
    BudgetGuard,
    Ledger,
    TicketError,
)
from jailbreak_testbed.events import Cost, Event, EventType  # noqa: E402


class BudgetGuardTest(unittest.TestCase):
    def test_reserve_then_settle_keeps_accounting(self):
        guard = BudgetGuard(Budget(target_calls=3, judge_calls=1, tokens=1000, wall_ms=10_000))
        t = guard.reserve("target_calls", 1)
        self.assertEqual(guard.remaining()["target_calls"], 2)  # 预留已占位
        cost = guard.settle(t, 1)
        self.assertEqual(cost.target_calls, 1)
        self.assertEqual(guard.remaining()["target_calls"], 2)
        self.assertEqual(guard.spent["target_calls"], 1)

    def test_reserve_beyond_budget_raises(self):
        guard = BudgetGuard(Budget(target_calls=1))
        guard.reserve("target_calls", 1)
        with self.assertRaises(BudgetExhausted):
            guard.reserve("target_calls", 1)

    def test_settle_twice_raises(self):
        guard = BudgetGuard(Budget())
        t = guard.reserve("judge_calls", 1)
        guard.settle(t, 1)
        with self.assertRaises(TicketError):
            guard.settle(t, 1)

    def test_unsettled_ticket_detected(self):
        guard = BudgetGuard(Budget())
        t = guard.reserve("target_calls", 1)
        with self.assertRaises(TicketError):
            guard.assert_settled([t])
        guard.settle(t, 1)
        guard.assert_settled([t])  # 不抛

    def test_unknown_kind_rejected(self):
        guard = BudgetGuard(Budget())
        with self.assertRaises(TicketError):
            guard.reserve("gpu_hours", 1)


class LedgerTest(unittest.TestCase):
    def _ev(self, seq, cost, served):
        return Event(seq=seq, type=EventType.RESPONSE, cost=cost, served=served)

    def test_aggregates_cost_and_counts_served(self):
        events = [
            self._ev(1, Cost(target_calls=1, tokens_out=10), True),
            self._ev(2, Cost(target_calls=1, tokens_out=10), False),  # 缓存命中
            self._ev(3, Cost(judge_calls=1, tokens_out=5), True),
        ]
        ledger = Ledger.from_events(events)
        self.assertEqual(ledger.cost.target_calls, 2)
        self.assertEqual(ledger.cost.judge_calls, 1)
        self.assertEqual(ledger.logical_calls, 3)
        self.assertEqual(ledger.served_calls, 2)  # served=false 不计入


if __name__ == "__main__":
    unittest.main()
