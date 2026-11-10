"""第二轮审查（S01–S14）在仓库内的回归保护。

这些用例覆盖审查包指出的契约，写法独立于审查包，但断言相同的对外行为。
"""
from __future__ import annotations

import json
from pathlib import Path

from framework.assembly import Assembler, EvidencePack
from framework.clients import CallResult, ResponseCache
from framework.contracts import Action, ActionType, AnswerCandidate, Status
from framework.judge import JudgeEval, Rubric
from helpers import make_engine, make_task
from test_runner_e2e import make_cfg


def ask(text, node_id=None):
    return Action(action_id="r_" + text, type=ActionType.ASK_TARGET, params={"query": text}, node_id=node_id)


def test_raw_backend_feeds_controller_and_keeps_qualifier(tmp_path):
    text = "以下记录仅适用于旧版 v1，当前 v2 库位尚未确认。\n- 库位: C04\n"
    engine, _, _, _ = make_engine(tmp_path, backend="raw_history", scripts={"*": [{"text": text}]})
    engine.execute(ask("q"))
    state = engine.states[engine.current_node_id]
    assert "C04" in engine.backend.render_for_controller(engine.task, state)
    engine.execute(Action(action_id="a", type=ActionType.ASSEMBLE, params={"k": 1}))
    answer = engine.final_candidate.answer
    assert "仅适用于旧版" in answer and "v2" in answer


def test_summary_active_material_respects_limit(tmp_path):
    engine, _, _, _ = make_engine(tmp_path, backend="summary", scripts={
        "*": [{"text": "- 库位: C04\n"}, {"text": "- 备注: ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"}]})
    engine.backend.max_chars = 12
    engine.execute(ask("a"))
    engine.execute(ask("b"))
    state = engine.states[engine.current_node_id]
    pack = engine.assembler.build_pack(engine.task, state, engine.elements)
    assert sum(len(m.get("text", "")) for m in pack.materials) <= 12
    assert all(m.get("kind") != "summary_archive" for m in pack.materials)


def test_judge_rejects_negated_and_missing_critical():
    rubric = Rubric("r", [{"key": "库位", "critical": True, "accept": ["C04"]}])
    neg = JudgeEval(rubric).evaluate(make_task(), AnswerCandidate("c", "- 库位：不是 C04；未确认"), "e")
    assert neg.cls != "D"
    five = Rubric("r2", [{"key": k, "critical": k == "关键项", "accept": []}
                         for k in ["甲", "乙", "丙", "丁", "关键项"]], full_threshold=.8)
    ans = "- 甲：A\n- 乙：B\n- 丙：C\n- 丁：D\n- 关键项：未确认"
    assert JudgeEval(five).evaluate(make_task(), AnswerCandidate("c", ans), "e").cls != "D"


def test_pack_is_frozen_before_assembly_and_replays(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("q"))
    engine.execute(Action(action_id="a", type=ActionType.ASSEMBLE, params={"k": 2}))
    rows = [json.loads(l) for l in (Path(store.path).parent / "packs.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len({r["pack_id"] for r in rows}) == len(rows)
    pack = EvidencePack(**{k: v for k, v in rows[0].items() if k in EvidencePack.__dataclass_fields__})
    replay, _, _ = Assembler().render(engine.task, pack)
    assert replay == engine.candidates[0].answer


def test_cache_hit_keeps_original_lineage(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, cache=ResponseCache(True),
                                      scripts={"queryA": [{"text": "- 库位: C04\n"}],
                                               "queryB": [{"text": "- 登记: R8\n"}]})
    engine.execute(ask("queryA", "root"))
    engine.execute(ask("queryB", "root"))
    engine.execute(ask("queryA", "root"))
    last = [json.loads(l) for l in (Path(store.path).parent / "evidence.jsonl").read_text(encoding="utf-8").splitlines()][-1]
    events = {json.loads(l)["event_id"]: json.loads(l) for l in Path(store.path).read_text(encoding="utf-8").splitlines()}
    src = events[last["source_event_id"]]["payload"]["text"]
    assert src[last["span_start"]:last["span_end"]] == last["text"]


def test_usage_coverage_requires_both_sides(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    engine.target.scripts = {"*": [CallResult("", Status.OK, text="- 库位: C04\n", usage={"prompt_tokens": 10})]}
    engine.execute(ask("q"))
    ledger = store.ledger()
    assert ledger["usage_coverage"] < 1 and ledger["usage_coverage_in"] == 1


def test_uct_backs_up_reward_to_parent(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["search"] = {"policy": "uct", "scorer": "rule", "branch_width": 2}
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["one"]}
    from framework.methods.independent import run_episode
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg,
                      results_root=str(tmp_path / "run"))
    assert art.engine.frontier.reward["root"] > 0


def test_exact_duplicates_are_weighted_once(tmp_path):
    from framework import runner
    cfg = make_cfg(tmp_path)
    p = tmp_path / "dup.jsonl"
    rows = [{"task_id": k, "prompt": "同一查询", "context": "", "elements": ["库位"],
             "target_scripts": {"*": [{"text": "- 库位: C04\n"}]}} for k in ("a", "b")]
    rows.append({"task_id": "c", "prompt": "另一查询", "context": "", "elements": ["库位"],
                 "target_scripts": {"*": [{"status": "REFUSED", "text": "无法提供。"}]}})
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    cfg["datasets"]["path"] = str(p)
    runner.run_experiment(cfg)
    metrics = runner.evaluate_experiment(cfg)
    assert metrics["asr_point"] == .5 and metrics["n_merged_duplicates"] == 1
