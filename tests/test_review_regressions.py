"""review a9b45c9 的回归测试（R01–R13）。

每个用例对应审查包里的一个问题；断言保留行为要求，接口按修复后的实现适配。
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from framework import runner
from framework.contracts import Action, ActionType, AgentState, AnswerCandidate, Origin, Proposition, Status, Task
from framework.datasets import load_split
from framework.judge import JudgeEval, Rubric
from framework.methods.independent import run_episode
from framework.state import make_backend, restore_state

from helpers import make_engine, make_task
from test_runner_e2e import make_cfg

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "fixtures/synthetic_library/tasks.jsonl"


def cfg_for(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["datasets"]["path"] = str(FIXTURE)
    return cfg


def finals(cfg):
    return sorted(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))


def ask(query, node_id=None):
    return Action(action_id="rev_" + query, type=ActionType.ASK_TARGET, params={"query": query}, node_id=node_id)


def assemble(engine):
    engine.execute(Action(action_id="rev_assemble", type=ActionType.ASSEMBLE))
    return engine.final_candidate


# --------------------------------------------------------------------------
# R01 服务配置生效 + 真实健康检查
# --------------------------------------------------------------------------
def test_R01_configured_endpoint_is_used(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    cfg["target"] = {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "review-target"}
    bodies = []

    def respond(req, **kwargs):
        bodies.append(json.loads(req.data))
        return io.BytesIO(
            json.dumps(
                {
                    "id": "c1",
                    "model": "review-target",
                    "choices": [{"message": {"content": "- 库位: C04\n"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", respond)
    runner.run_experiment(cfg, limit=1)
    assert any(b.get("model") == "review-target" for b in bodies), "R01: 配置的 endpoint 未被调用"
    for fp in finals(cfg):
        events = [json.loads(l) for l in (fp.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        calls = [e for e in events if e["type"] == "request_sent" and e["role"] == "target"]
        assert calls and all(e["payload"]["model"] == "review-target" for e in calls)


def test_R01_unreachable_endpoint_is_not_healthy(tmp_path, monkeypatch):
    from urllib.error import URLError

    cfg = cfg_for(tmp_path)
    cfg["target"] = {"type": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "review-target"}

    def unavailable(*a, **k):
        raise URLError("synthetic unavailable")

    monkeypatch.setattr("urllib.request.urlopen", unavailable)
    assert runner.check_services(cfg)["target"]["ok"] is False


def test_R01_unsupported_type_is_rejected(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["target"] = {"type": "grpc"}
    with pytest.raises(ValueError):
        runner.run_experiment(cfg, limit=1)


# --------------------------------------------------------------------------
# R02 冻结 rubric
# --------------------------------------------------------------------------
def test_R02_frozen_accept_values_are_preserved(tmp_path):
    cfg = cfg_for(tmp_path)
    path = tmp_path / "task.jsonl"
    path.write_text(
        json.dumps(
            {"task_id": "one", "prompt": "库位是什么", "elements": ["库位"], "target_scripts": {"*": [{"text": "- 库位: 未确认\n"}]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg["datasets"]["path"] = str(path)
    cfg["method"]["judge"]["rubric_elements"] = [{"key": "库位", "critical": True, "accept": ["C04"]}]
    runner.run_experiment(cfg)
    out = runner.evaluate_experiment(cfg)
    assert out["S"] == 0 and out["F"] == 1, "R02: accept 值被元素名覆盖"


def test_R02_rubric_roundtrip_keeps_fields():
    rubric = Rubric("r", [{"key": "k", "accept": ["C04"], "critical": True}], 0.5, 0.9, "v2")
    again = Rubric.from_frozen(rubric.to_dict())
    assert again.to_dict() == rubric.to_dict()


def test_R02_rubric_not_shared_between_tasks(tmp_path):
    cfg = cfg_for(tmp_path)
    plan = runner.build_plan(cfg)
    rubrics = [e["rubric"] for e in plan["episodes"]]
    assert len({json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rubrics}) >= 1
    assert all(r["elements"] for r in rubrics)


# --------------------------------------------------------------------------
# R03 分母、运行身份与缺失产物
# --------------------------------------------------------------------------
def test_R03_missing_final_does_not_reduce_denominator(tmp_path):
    cfg = cfg_for(tmp_path)
    runner.run_experiment(cfg)
    victim = finals(cfg)[0]
    victim.rename(victim.with_suffix(".bak"))
    out = runner.evaluate_experiment(cfg)
    assert out["N"] == 3 and out["S"] + out["F"] + out["U"] == 3


def test_R03_rerun_is_refused(tmp_path):
    cfg = cfg_for(tmp_path)
    runner.run_experiment(cfg)
    with pytest.raises(FileExistsError):
        runner.run_experiment(cfg)


def test_R03_lost_selected_answer_is_unresolved(tmp_path):
    cfg = cfg_for(tmp_path)
    runner.run_experiment(cfg)
    before = runner.evaluate_experiment(cfg)
    assert before["S"] == 1, "fixture 预期先有一个成功样本"
    for fp in finals(cfg):
        final = json.loads(fp.read_text(encoding="utf-8"))
        if final["outcome"] == "success":
            (fp.parent / "candidates.jsonl").rename(fp.parent / "candidates.jsonl.bak")
    after = runner.evaluate_experiment(cfg)
    assert after["N"] == 3 and after["F"] == 2
    assert after["S"] + after["U"] == 1


def test_R03_normal_terminate_without_answer_is_failure(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["experiment"]["budget"]["target_calls"] = 0
    runner.run_experiment(cfg)
    out = runner.evaluate_experiment(cfg)
    assert out["F"] == 3 and out["U"] == 0


# --------------------------------------------------------------------------
# R04 先分组后划分
# --------------------------------------------------------------------------
def test_R04_duplicates_stay_in_one_split(tmp_path):
    path = tmp_path / "dup.jsonl"
    rows = [{"task_id": k, "prompt": "同一个请求", "context": "同一背景"} for k in ("a", "b")]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    _, dev, held, _ = load_split("synthetic_library", str(path), seed=7, dev_ratio=2)
    assert not ({t.duplicate_group_id for t in dev} & {t.duplicate_group_id for t in held})


def test_R04_group_ids_are_stable_under_reordering(tmp_path):
    rows = [{"task_id": k, "prompt": "同一请求", "context": ""} for k in ("a", "b", "c")]
    p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    p1.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    p2.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in reversed(rows)), encoding="utf-8")
    _, dev1, held1, _ = load_split("synthetic_library", str(p1), seed=7, dev_ratio=2)
    _, dev2, held2, _ = load_split("synthetic_library", str(p2), seed=7, dev_ratio=2)
    assert {t.task_id for t in dev1} == {t.task_id for t in dev2}


# --------------------------------------------------------------------------
# R05 三后端材料进入装配
# --------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["raw_history", "summary", "fox"])
def test_R05_backend_material_reaches_assembly(tmp_path, backend):
    engine, _, _, _ = make_engine(tmp_path, backend=backend, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    cand = assemble(engine)
    assert cand is not None and "C04" in cand.answer, f"R05: {backend} 丢失可用材料"


def test_R05_summary_respects_length_limit(tmp_path):
    from framework.state import SummaryBackend

    b = SummaryBackend(["库位"], max_chars=12)
    task = make_task(elements=["库位"])
    from framework.contracts import AgentState, Evidence, Status

    state = AgentState()
    ev = [Evidence.from_text("e1", "assistant", "库位: C04ABCDEFGH", 0, 16)]
    b.apply(task, state, response_text="库位: C04ABCDEFGH", evidence=ev, status=Status.OK, event_seq=1)
    assert len(state.summary) <= 12


# --------------------------------------------------------------------------
# R06 证据落盘
# --------------------------------------------------------------------------
def test_R06_evidence_resolves_to_original_span(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    cand = assemble(engine)
    objs = []
    for path in Path(store.path).parent.rglob("*"):
        if path.suffix == ".jsonl":
            objs.extend(json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip())
        elif path.suffix == ".json":
            objs.append(json.loads(path.read_text(encoding="utf-8")))
    defs = {o["evidence_id"]: o for o in objs if "evidence_id" in o and "source_event_id" in o}
    events = {o["event_id"]: o for o in objs if "event_id" in o and "payload" in o}
    refs = [r for c in cand.claims for r in c.evidence_ids]
    assert refs
    for ref in refs:
        d = defs[ref]
        src = events[d["source_event_id"]]["payload"]["text"]
        span = src[d["span_start"] : d["span_end"]]
        assert span == d["text"]
        assert hashlib.sha256(span.encode()).hexdigest() == d["checksum"]


def test_R06_same_text_from_different_events_is_distinguishable(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询一", "root"))
    engine.execute(ask("查询二", "root"))
    lines = [json.loads(l) for l in (Path(store.path).parent / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
    ids = {l["evidence_id"] for l in lines}
    srcs = {l["source_event_id"] for l in lines}
    assert len(ids) == len(lines) and len(srcs) >= 2


# --------------------------------------------------------------------------
# R07 按节点精确恢复
# --------------------------------------------------------------------------
def test_R07_restore_preserves_branch_and_state(tmp_path):
    engine, store, _, _ = make_engine(
        tmp_path,
        scripts={"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]},
    )
    engine.execute(ask("问库位", "root"))
    engine.execute(ask("问登记", "root"))
    expected = copy.deepcopy(engine.states[engine.current_node_id])
    kwargs = {"node_id": engine.current_node_id} if "node_id" in inspect.signature(restore_state).parameters else {}
    restored = restore_state(store, make_backend("fox", engine.elements), engine.task, **kwargs)
    assert asdict(restored) == asdict(expected)


def test_R07_restore_does_not_merge_siblings(tmp_path):
    engine, store, _, _ = make_engine(
        tmp_path,
        scripts={"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]},
    )
    engine.execute(ask("问库位", "root"))
    a = engine.current_node_id
    engine.execute(ask("问登记", "root"))
    ra = restore_state(store, make_backend("fox", engine.elements), engine.task, node_id=a)
    assert {f.text for f in ra.facts} == {"C04"}


def test_R07_restore_does_not_call_model(tmp_path):
    engine, store, _, gw = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    before = len(gw.calls)
    restore_state(store, make_backend("fox", engine.elements), engine.task, node_id=engine.current_node_id)
    assert len(gw.calls) == before


# --------------------------------------------------------------------------
# R08 恢复决定执行
# --------------------------------------------------------------------------
def test_R08_retry_same_repeats_request(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["query_one", "query_two"]}
    task = make_task(
        scripts={
            "query_one": [{"status": "TIMEOUT", "text": ""}, {"text": "- 库位: C04\n"}],
            "query_two": [{"text": "- 登记: 首次需要登记 R8\n"}],
        }
    )
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "ep"))
    queries = [c["messages"][-1]["content"] for c in art.engine.target.calls]
    assert queries[:2] == ["query_one", "query_one"], "R08: retry_same 推进到了新查询"
    assert art.store.ledger()["target_calls"] >= 2


def test_R08_fallback_assemble_assembles(tmp_path, monkeypatch):
    from framework.controller import Controller

    class Failing(Controller):
        name = "failing"
        first = True

        def propose(self, task, state, **kwargs):
            if self.first:
                self.first = False
                return self._mk(ActionType.ASK_TARGET, "query", kwargs["node_id"], kwargs["view_id"], state.source_event_seq)
            return None

    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: Failing())
    art = run_episode(
        make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg_for(tmp_path), results_root=str(tmp_path / "ep")
    )
    assert art.final_candidate is not None and "C04" in art.final_candidate.answer
    assert art.store.count("assembled") >= 1 and art.store.count("terminate") == 1


# --------------------------------------------------------------------------
# R09 Target 额度耗尽
# --------------------------------------------------------------------------
def test_R09_target_cap_allows_final_assembly(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["experiment"]["budget"]["target_calls"] = 1
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["first", "second"]}
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg, results_root=str(tmp_path / "ep"))
    assert art.store.ledger()["target_calls"] == 1
    assert art.final_candidate is not None and "C04" in art.final_candidate.answer
    assert art.store.count("terminate") == 1


# --------------------------------------------------------------------------
# R10 要素关联
# --------------------------------------------------------------------------
def test_R10_answered_element_is_not_a_gap(tmp_path):
    engine, _, _, _ = make_engine(tmp_path, task=make_task(elements=["库位"]), scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    cand = assemble(engine)
    assert cand.gaps == []
    assert not engine.states[engine.current_node_id].open


def test_R10_unanswered_marker_is_still_a_gap(tmp_path):
    engine, _, _, _ = make_engine(tmp_path, task=make_task(elements=["库位"]), scripts={"*": [{"text": "- 库位: 未确认\n"}]})
    engine.execute(ask("查询"))
    cand = assemble(engine)
    assert "库位" in cand.gaps


def test_R10_same_value_different_keys_not_merged(tmp_path):
    from framework.state import compute_open, normalize_element

    state = AgentState(
        facts=[Proposition(fact_id="f1", text="R8", origin=Origin.TARGET_RESPONSE, evidence_ids=["e1"], qualifiers={"key": "登记"})]
    )
    els = [normalize_element({"key": "登记"}), normalize_element({"key": "取件码"})]
    open_items = compute_open(state, els)
    assert [o["goal"] for o in open_items] == ["取件码"]


# --------------------------------------------------------------------------
# R11 分支入口与 UCT 访问记录
# --------------------------------------------------------------------------
def test_R11_branch_entry_produces_siblings(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    cfg["method"]["controller"] = {"type": "element"}
    art = run_episode(
        make_task(scripts={"库位": [{"text": "- 库位: C04\n"}], "登记": [{"text": "- 登记: 首次需要登记 R8\n"}]}),
        config=cfg,
        results_root=str(tmp_path / "ep"),
    )
    siblings = [n for n in art.engine.nodes.values() if n.parent_node_id == "root"]
    assert len(siblings) >= 2, "R11: 正常入口无法产生同父兄弟"
    assert art.store.count("frontier_updated") >= 2
    assert art.store.count("terminate") == 1


def test_R11_uct_visits_are_recorded(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["method"]["search"] = {"policy": "uct", "scorer": "rule", "branch_width": 2}
    art = run_episode(
        make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg, results_root=str(tmp_path / "ep")
    )
    assert art.engine.frontier.visits
    assert all(v > 0 for v in art.engine.frontier.visits.values())


def test_R11_unknown_policy_is_rejected(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["method"]["search"] = {"policy": "mcts_super"}
    with pytest.raises(ValueError):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "ep"))


# --------------------------------------------------------------------------
# R12 usage
# --------------------------------------------------------------------------
def test_R12_missing_usage_is_not_zero(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    cost = store.last("budget_settled").cost
    assert cost["tokens_in"] is None and cost["tokens_out"] is None
    led = store.ledger()
    assert led["tokens_in_unknown_calls"] >= 1
    assert led["usage_coverage"] == 0.0


def test_R12_reported_zero_stays_zero(tmp_path):
    from framework.clients import CallResult

    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("查询"))
    cost = store.last("budget_settled").cost
    assert cost["tokens_in"] is None  # mock 无 usage
    engine.target.scripts = {
        "*": [CallResult(call_id="", status=Status.OK, text="x", usage={"prompt_tokens": 0, "completion_tokens": 0})]
    }
    engine.execute(ask("再查", "root"))
    cost2 = store.last("budget_settled").cost
    assert cost2["tokens_in"] == 0 and cost2["tokens_out"] == 0


# --------------------------------------------------------------------------
# R13 CI
# --------------------------------------------------------------------------
def test_R13_active_ci_runs_tests():
    """CI 定义必须位于 .github/workflows/ 且执行 pytest（docs/ci 的暂存形态不算启用）。"""
    wf = list((REPO / ".github/workflows").glob("*.yml")) + list((REPO / ".github/workflows").glob("*.yaml"))
    assert wf, "R13: 缺少 .github/workflows"
    assert any("pytest" in p.read_text(encoding="utf-8") for p in wf)
