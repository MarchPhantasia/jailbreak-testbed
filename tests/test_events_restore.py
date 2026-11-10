"""事件、恢复与状态重建（手册 §13、§18.1）。"""

from __future__ import annotations

import json

from framework.contracts import Action, ActionType, Status
from framework.state import make_backend, restore_state

from helpers import make_engine, make_task


def act(query: str, node_id: str | None = None) -> Action:
    return Action(action_id=f"a_{query[:4]}", type=ActionType.ASK_TARGET, params={"query": query}, node_id=node_id)


def test_single_terminate_event(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(act("查询一"))
    engine.terminate("stop_action")
    engine.terminate("stop_action")  # 重复调用不新增事件
    assert store.count("terminate") == 1


def test_restore_state_from_events_without_model(tmp_path):
    scripts = {"*": [{"text": "- 库位: C04\n- 登记: 首次需要登记 R8，以后不用\n"}]}
    engine, store, _, gw = make_engine(tmp_path, scripts=scripts)
    engine.execute(act("查询库位"))
    live_state = engine.states[engine.current_node_id]
    calls_before = len(gw.calls)

    restored = restore_state(store, make_backend("fox", engine.elements), engine.task)
    assert [f.text for f in restored.facts] == [f.text for f in live_state.facts]
    assert len(gw.calls) == calls_before  # 恢复不调用模型
    assert restored.source_event_seq > 0


def test_state_write_records_extractor_output(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(act("查询库位"))
    writes = store.of_type("state_write")
    assert writes, "必须落 state_write"
    payload = writes[-1].payload
    assert payload["backend"] == "fox"
    assert any(f["text"] == "C04" for f in payload["new_facts"])
    assert payload["state_seq"] > 0


def test_fact_without_evidence_goes_to_hypotheses(tmp_path):
    # 该行无法在原始回复里定位（模拟抽取器凭空产生）→ 只能进 hypotheses
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "这里没有任何键值行\n"}]})
    engine.execute(act("查询库位"))
    state = engine.states[engine.current_node_id]
    assert all(f.evidence_ids for f in state.facts)
    assert state.hypotheses or not state.facts


def test_events_are_append_only(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(act("查询一"))
    before = store.count("response_received")
    engine.execute(act("查询二"))
    lines = open(store.path, "r", encoding="utf-8").read().strip().splitlines()
    seqs = [json.loads(l)["seq"] for l in lines]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    assert store.count("response_received") == before + 1
