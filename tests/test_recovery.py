"""恢复策略与"不编造"约束（手册 §11、§18.1）。"""

from __future__ import annotations

from framework.contracts import Action, ActionType, Status
from framework.events import EventStore
from framework.recovery import RecoveryPolicy

from helpers import make_engine


def test_network_retry_is_limited(tmp_path):
    store = EventStore(str(tmp_path), "ep_r")
    pol = RecoveryPolicy(store, max_network_retries=1, total_cap=5)
    assert pol.handle("target", Status.TIMEOUT).action == "retry_same"
    assert pol.handle("target", Status.TIMEOUT).action == "fallback_continue"


def test_recovery_cap_stops(tmp_path):
    store = EventStore(str(tmp_path), "ep_r2")
    pol = RecoveryPolicy(store, total_cap=1)
    pol.handle("target", Status.TIMEOUT)
    assert pol.handle("target", Status.TRANSPORT_ERROR).action == "stop"


def test_format_failure_falls_back_to_assemble(tmp_path):
    store = EventStore(str(tmp_path), "ep_r3")
    pol = RecoveryPolicy(store, max_format_retries=1)
    assert pol.handle("controller", Status.INVALID_OUTPUT).action == "retry_rephrase"
    assert pol.handle("controller", Status.INVALID_OUTPUT).action == "fallback_assemble"


def test_refusal_does_not_fabricate_facts(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"status": "REFUSED", "text": "抱歉，我不能提供这方面的信息。"}]})
    engine.execute(Action(action_id="a1", type=ActionType.ASK_TARGET, params={"query": "查询"}))
    state = engine.states[engine.current_node_id]
    assert state.facts == []
    assert all(f.origin.value == "target_response" for f in state.facts)
    assert any(x.get("reason") == "REFUSED" for x in state.failed)


def test_no_verifier_calls_when_verification_disabled(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(Action(action_id="a1", type=ActionType.ASK_TARGET, params={"query": "查询"}))
    roles = {e.role for e in store.of_type("request_sent")}
    assert "verifier" not in roles
    assert roles <= {"target"}


def test_unknown_status_terminates_without_success(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"status": "TRANSPORT_ERROR", "text": ""}]})
    res = engine.execute(Action(action_id="a1", type=ActionType.ASK_TARGET, params={"query": "查询"}))
    assert res.status is Status.TRANSPORT_ERROR
    assert engine.final_candidate is None
