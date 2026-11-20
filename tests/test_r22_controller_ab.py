import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import r22_controller_ab as probe

from framework.budget import BudgetGuard
from framework.clients import CallResult, MockGateway
from framework.contracts import BudgetLimits, Status
from framework.events import EventStore


def publication_case():
    return {"id": "paper_title", "visible": {"card": probe.PAPER,
            "previously_requested": probe.PAPER["fields"],
            "received": {"text": "Title only", "evidence_id": "ev_fixture", "start": 0, "end": 10}},
            "expected": ["发表年份"]}


def test_scores_delivered_question_separately_from_planned_fields():
    case = publication_case()
    plan = {"decision": "ask", "fields": ["发表年份"], "note": "保留作者和材料口径"}
    clean = probe.evaluate(case, plan, "按原文，Clough & Stevenson 研究的【发表年份】是什么？")
    assert clean["plan_correct"] and clean["request_correct"] and not clean["reasked_known"]
    repeat = probe.evaluate(case, plan, "按原文，Clough & Stevenson 研究的【期刊全名】和【发表年份】是什么？")
    assert repeat["plan_correct"] and not repeat["request_correct"] and repeat["reasked_known"] == ["期刊全名"]
    unknown = probe.evaluate(case, plan, "请给出年份。")
    assert unknown["request_correct"] is None and unknown["reasked_known"] is None
    early_stop = probe.evaluate(case, {"decision": "done", "fields": [], "note": ""}, "")
    assert early_stop["request_correct"] is False and early_stop["missing_in_question"] == ["发表年份"]


def test_field_match_alone_does_not_satisfy_scope_or_direction():
    case = {"visible": {"card": probe.GRANT}, "expected": ["计时基准日"]}
    plan = {"decision": "ask", "fields": ["计时基准日"], "note": ""}
    for question in ("按 NSF CAREER 原文，请给出此前提过的【计时基准日】？",
                     "按 NSF CAREER 原文，是向后还是向前推算【计时基准日】？"):
        result = probe.evaluate(case, plan, question)
        assert result["requested_fields_correct"] and result["source_scope"] and not result["backward_direction"]
        assert not result["request_correct"]


def test_offline_report_explains_markers_without_changing_result(tmp_path):
    case = publication_case()
    (tmp_path / "protocol.json").write_text(json.dumps({"cases": [case], "source_terms": ["材料"]}), encoding="utf-8")
    plan = {"decision": "ask", "fields": ["发表年份"], "note": ""}
    for arm, question in (("A", "按材料，请告知发表年份？"), ("B", "请提供【发表年份】，核对【发表年份】。")):
        data = {"question": question, "plan": plan, "calls": [], "elapsed_ms": 0,
                "grade": probe.evaluate(case, plan, question)}
        (tmp_path / f"paper_title-{arm}.json").write_text(json.dumps(data), encoding="utf-8")
    snapshots = {f: f.read_bytes() for f in tmp_path.iterdir()}
    report = probe.read_report(tmp_path)
    a, b = report["wording_notes"]
    assert a["planned_fields_in_text_without_marker"] == ["发表年份"] and a["explicit_source_term"] == "材料"
    assert b["repeated_markers"] == ["发表年份"]
    assert report["paired_requests"]["unscored"] == ["paper_title"]
    assert all(path.read_bytes() == original for path, original in snapshots.items())


def test_split_sends_only_handoff_then_skips_writer_on_done(tmp_path):
    case = publication_case()
    plan = {"decision": "ask", "fields": ["发表年份"], "note": "按原文询问年份"}
    question = {"question": "按资料所述，Clough & Stevenson 的【发表年份】是哪年？"}
    done = {"decision": "done", "fields": [], "note": "齐全"}
    store = EventStore(str(tmp_path), "ep")
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=3), store, "ep")
    responses = [CallResult("", Status.OK, text=json.dumps(o, ensure_ascii=False)) for o in (plan, question, done)]
    gateway = MockGateway(guard, store, scripts={"*": responses})
    gateway.stage_id = "plan"
    ask = probe.run_arm(case, "B", gateway, store)
    assert ask["grade"]["request_correct"] and len(ask["calls"]) == 2
    calls = [e for e in store.events if e.type == "request_sent"]
    planner = json.loads(calls[0].payload["messages"][0]["content"].split("\n", 1)[1])
    writer = json.loads(calls[1].payload["messages"][0]["content"].split("\n", 1)[1])
    assert planner == case["visible"] and writer == {"record": "Clough & Stevenson", "plan": plan}
    assert probe.interpret_plan({**plan, "decision": "done"}, probe.PAPER["fields"]) is None
    closed = probe.run_arm({**case, "id": "complete", "expected": []}, "B", gateway, store)
    assert closed["grade"]["request_correct"] and len(closed["calls"]) == 1
    paired = probe.summarize({"title": {"A": ask, "B": ask}, "closed": {"A": closed, "B": closed}})
    assert paired["paired_requests"]["both_correct"] == ["title", "closed"]
    assert paired["by_arm"]["B"]["tokens_in"] is None
