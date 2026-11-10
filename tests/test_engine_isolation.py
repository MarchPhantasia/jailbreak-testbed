"""节点隔离、View 正确性、证据与评分（手册 §7.1、§7.2、§18.1）。"""

from __future__ import annotations

from framework.contracts import Action, ActionType, NodeStatus

from helpers import make_engine, make_task


def ask(query: str, node_id: str | None = None) -> Action:
    return Action(action_id=f"a_{query[:4]}", type=ActionType.ASK_TARGET, params={"query": query}, node_id=node_id)


def test_sibling_state_is_isolated(tmp_path):
    scripts = {"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]}
    engine, store, _, _ = make_engine(tmp_path, scripts=scripts)
    engine.execute(ask("问库位", node_id="root"))
    a = engine.current_node_id
    engine.execute(ask("问登记", node_id="root"))
    b = engine.current_node_id
    assert a != b
    assert engine.nodes[a].parent_node_id == engine.nodes[b].parent_node_id == "root"
    texts_a = {f.text for f in engine.states[a].facts}
    texts_b = {f.text for f in engine.states[b].facts}
    assert texts_a != texts_b
    assert "C04" in texts_a and "首次需要登记 R8" in texts_b
    assert "首次需要登记 R8" not in texts_a  # 兄弟状态不共享


def test_view_only_contains_ancestor_turns(tmp_path):
    """子视图 = 祖先轮次 + 本次请求；不能看到兄弟分支的内容。"""
    scripts = {"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]}
    engine, store, _, _ = make_engine(tmp_path, scripts=scripts)
    engine.execute(ask("问库位", node_id="root"))
    child_a = engine.current_node_id
    engine.execute(ask("问登记", node_id="root"))  # 兄弟分支
    engine.execute(ask("追问库位细节", node_id=child_a))

    snaps = store.of_type("view_snapshot")
    first = [m["content"] for m in snaps[0].payload["messages"]]
    sibling = [m["content"] for m in snaps[1].payload["messages"]]
    grandchild = [m["content"] for m in snaps[2].payload["messages"]]

    assert "问库位" in first and "问登记" not in first
    assert "问登记" in sibling and "问库位" not in sibling  # 兄弟分支互不可见
    assert any("问库位" in m for m in grandchild)  # 祖先轮次可见
    assert any("C04" in m for m in grandchild)  # 祖先的回复可见
    assert "问登记" not in " ".join(grandchild)  # 兄弟内容不泄漏
    assert grandchild[-1] == "追问库位细节"


def test_every_fact_has_locatable_evidence(tmp_path):
    response = "- 库位: C04\n- 登记: 首次需要登记 R8，以后不用\n"
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": response}]})
    engine.execute(ask("查询"))
    state = engine.states[engine.current_node_id]
    assert state.facts
    for f in state.facts:
        assert f.evidence_ids
        assert f.text in response  # 原文可回查
        assert f.origin.value == "target_response"


def test_qualifiers_are_preserved(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 登记: 首次需要登记 R8，以后不用\n"}]})
    engine.execute(ask("查询登记"))
    fact = engine.states[engine.current_node_id].facts[0]
    assert "首次" in fact.text and "以后不用" in fact.text  # 不把条件写成无条件的


def test_unknown_score_is_none_not_zero(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]}, scorer="null")
    engine.execute(ask("查询"))
    child = engine.current_node_id
    assert engine.frontier.score(child) is None
    rec = store.of_type("score_recorded")[-1]
    assert rec.payload["score"] is None
    assert "node_score" in rec.payload["unknown"]


def test_rule_scorer_produces_bounded_score(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]}, scorer="rule")
    engine.execute(ask("查询"))
    s = engine.frontier.score(engine.current_node_id)
    assert s is not None and 0.0 <= s <= 1.0


def test_branching_policy_can_pick_other_node(tmp_path):
    scripts = {"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]}
    engine, store, _, _ = make_engine(tmp_path, scripts=scripts, policy="best_first", scorer="rule")
    engine.execute(ask("问库位", node_id="root"))
    engine.execute(ask("问登记", node_id="root"))
    assert len(engine.frontier) == 2
    picked = engine.select_node()
    assert picked in engine.frontier.ids()
    assert store.count("node_selected") == 1
