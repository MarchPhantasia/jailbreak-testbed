"""预算与缓存的不变量（手册 §8.2、§8.3、§18.1）。"""

from __future__ import annotations

import pytest

from framework.budget import BudgetExhausted, BudgetGuard
from framework.clients import ResponseCache
from framework.contracts import Action, ActionType, BudgetLimits, Status
from framework.events import EventStore

from helpers import make_engine


def act(type_: ActionType, query: str = "", node_id: str | None = None) -> Action:
    return Action(action_id=f"a_{type_.value}_{query[:6]}", type=type_, params={"query": query} if query else {}, node_id=node_id)


def test_target_budget_cannot_be_exceeded(tmp_path):
    engine, store, guard, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]}, target_calls=2)
    statuses = [engine.execute(act(ActionType.ASK_TARGET, f"问题{i}")).status for i in range(5)]
    assert statuses[:2] == [Status.OK, Status.OK]
    assert statuses[2] is Status.BUDGET_EXHAUSTED
    assert guard.target_calls_used() == 2
    assert engine.termination_reason == "budget_exhausted"


def test_refusal_still_consumes_target_budget(tmp_path):
    engine, store, guard, _ = make_engine(
        tmp_path,
        scripts={"*": [{"status": "REFUSED", "text": "抱歉，我不能提供这方面的信息。"}]},
        target_calls=3,
    )
    res = engine.execute(act(ActionType.ASK_TARGET, "库位是什么"))
    assert res.status is Status.REFUSED
    assert guard.target_calls_used() == 1
    assert store.ledger()["target_calls"] == 1
    assert store.count("target_refusal") == 1


def test_branch_switch_does_not_refund(tmp_path):
    engine, store, guard, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]}, target_calls=4)
    engine.execute(act(ActionType.ASK_TARGET, "第一次查询", node_id="root"))
    used_after_first = guard.target_calls_used()
    # 从根节点再开一个分支
    engine.execute(act(ActionType.ASK_TARGET, "第二次查询", node_id="root"))
    assert guard.target_calls_used() == used_after_first + 1
    released = [e for e in store.of_type("budget_settled") if e.payload.get("released")]
    assert released == []


def test_release_only_before_dispatch(tmp_path):
    store = EventStore(str(tmp_path), "ep_x")
    guard = BudgetGuard(BudgetLimits(target_calls=1), store, "ep_x")
    t1 = guard.reserve("target")
    guard.release(t1)  # 未发出可释放
    t2 = guard.reserve("target")
    guard.dispatch(t2)
    with pytest.raises(RuntimeError):
        guard.release(t2)  # 已发出不退款


def test_double_settle_rejected(tmp_path):
    store = EventStore(str(tmp_path), "ep_y")
    guard = BudgetGuard(BudgetLimits(target_calls=2), store, "ep_y")
    t = guard.reserve("target")
    guard.dispatch(t)
    guard.settle(t, {"tokens_in": 1}, call_id="c1")
    with pytest.raises(RuntimeError):
        guard.settle(t, {"tokens_in": 1}, call_id="c1")
    t2 = guard.reserve("target")
    guard.dispatch(t2)
    with pytest.raises(RuntimeError):
        guard.settle(t2, {}, call_id="c1")


def test_cache_hit_is_not_a_served_call(tmp_path):
    cache = ResponseCache(enabled=True)
    engine, store, guard, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]}, cache=cache)
    engine.execute(act(ActionType.ASK_TARGET, "同样的查询", node_id="root"))
    engine.execute(act(ActionType.ASK_TARGET, "同样的查询", node_id="root"))
    led = store.ledger()
    assert led["target_calls"] == 1
    assert led["served_calls"] == 1
    assert led["cache_hits"] == 1
    assert guard.target_calls_used() == 1
