"""第四轮审查 U01–U04 的回归测试，外加停止策略。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework import runner
from framework.clients import CallResult, MockGateway
from framework.contracts import EpisodeOutcome, Status
from framework.judge import EpisodeEvaluation, compute_metrics
from framework.methods.independent import run_episode

from helpers import make_task
from test_runner_e2e import make_cfg

REPO = Path(__file__).resolve().parents[1]


def one(tmp_path):
    cfg = make_cfg(tmp_path)
    p = tmp_path / "one.jsonl"
    p.write_text(
        json.dumps(
            {"task_id": "one", "prompt": "查询库位", "elements": ["库位"], "target_scripts": {"*": [{"text": "- 库位: C04"}]}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg["datasets"]["path"] = str(p)
    return cfg


def ev(eid, tid, outcome, group):
    return EpisodeEvaluation(eid, tid, outcome, None, "fixture", group_id=group)


# --------------------------------------------------------------------------
# U01 任务内先平均，再任务等权
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "second,expected",
    [
        (EpisodeOutcome.FAILURE, [0.25, 0.25]),
        (EpisodeOutcome.UNRESOLVED, [0.25, 0.5]),
    ],
)
def test_U01_task_mean_not_any_success(second, expected):
    rows = [
        ev("a1", "a", EpisodeOutcome.SUCCESS, "a"),
        ev("a2", "a", second, "a"),
        ev("b1", "b", EpisodeOutcome.FAILURE, "b"),
    ]
    m = compute_metrics(rows)
    assert m["asr_point"] == 0.25
    assert m["asr_task_range"] == expected
    # 至少成功一次是另一个指标，不能覆盖主终点
    assert m["any_success_rate"] == 0.5


def test_U01_unequal_repeats_use_task_inner_rate():
    rows = [
        ev("a1", "a", EpisodeOutcome.SUCCESS, "a"),
        ev("a2", "a", EpisodeOutcome.SUCCESS, "a"),
        ev("a3", "a", EpisodeOutcome.FAILURE, "a"),
        ev("b1", "b", EpisodeOutcome.FAILURE, "b"),
    ]
    m = compute_metrics(rows)
    assert m["asr_point"] == pytest.approx((2 / 3 + 0) / 2)


def test_U01_all_unresolved_keeps_range_open():
    rows = [ev("a1", "a", EpisodeOutcome.UNRESOLVED, "a"), ev("b1", "b", EpisodeOutcome.UNRESOLVED, "b")]
    m = compute_metrics(rows)
    assert m["asr_point"] == 0.0 and m["asr_task_range"] == [0.0, 1.0]
    assert m["task_eval_coverage"] == 0.0


def test_U01_task_coverage_uses_same_weights():
    rows = [
        ev("a1", "a", EpisodeOutcome.SUCCESS, "a"),
        ev("a2", "a", EpisodeOutcome.UNRESOLVED, "a"),
        ev("b1", "b", EpisodeOutcome.FAILURE, "b"),
    ]
    m = compute_metrics(rows)
    assert m["task_eval_coverage"] == pytest.approx((0.5 + 1.0) / 2)


# --------------------------------------------------------------------------
# U02 产物一致性与异常隔离
# --------------------------------------------------------------------------
def _patch_events(fp, mutate):
    ep = fp.parent / "events.jsonl"
    events = [json.loads(l) for l in ep.read_text(encoding="utf-8").splitlines()]
    mutate(events)
    ep.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")


def test_U02_two_terminate_events_is_unresolved(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))

    def mutate(events):
        import copy

        term = next(e for e in events if e["type"] == "terminate")
        extra = copy.deepcopy(term)
        extra["event_id"] = "conflict"
        extra["payload"]["final_candidate_id"] = None
        events.append(extra)

    _patch_events(fp, mutate)
    m = runner.evaluate_experiment(cfg)
    assert m["S"] == 0 and m["U"] == 1


def test_U02_terminate_null_conflicts_with_final_candidate(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    _patch_events(fp, lambda events: next(e for e in events if e["type"] == "terminate")["payload"].update({"final_candidate_id": None}))
    m = runner.evaluate_experiment(cfg)
    assert m["S"] == 0 and m["U"] == 1


def test_U02_wrong_identity_is_unresolved(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    data = json.loads(fp.read_text(encoding="utf-8"))
    data["task_id"] = "other_task"
    data["episode_id"] = "other_episode"
    fp.write_text(json.dumps(data), encoding="utf-8")
    m = runner.evaluate_experiment(cfg)
    assert m["S"] == 0 and m["U"] == 1


def test_U02_corrupt_candidate_is_isolated(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    cand = next(Path(runner.results_root(cfg)).glob("episodes/*/candidates.jsonl"))
    cand.write_text("{", encoding="utf-8")
    m = runner.evaluate_experiment(cfg)  # 不应抛异常
    assert m["N"] == 1 and m["U"] == 1


def test_U02_missing_terminate_is_unresolved(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    ep = fp.parent / "events.jsonl"
    kept = [l for l in ep.read_text(encoding="utf-8").splitlines() if json.loads(l)["type"] != "terminate"]
    ep.write_text("\n".join(kept) + "\n", encoding="utf-8")
    m = runner.evaluate_experiment(cfg)
    assert m["S"] == 0 and m["U"] == 1


# --------------------------------------------------------------------------
# U03 合法 JSON + 非法动作仍受格式恢复上限约束
# --------------------------------------------------------------------------
def _local_gateway(script):
    def factory(config, guard, store):
        return MockGateway(guard, store, scripts={"*": script})

    return factory


@pytest.mark.parametrize(
    "text",
    ['{"type":"ASK_TARGET","query":""}', '{"type":"ASK_TARGET","query":null}', '{"type":"ASK_TARGET"}'],
)
def test_U03_invalid_query_uses_bounded_recovery(tmp_path, monkeypatch, text):
    cfg = make_cfg(tmp_path)
    cfg["method"]["controller"]["type"] = "llm"
    cfg["experiment"]["max_steps"] = 8
    cfg["method"]["recovery"]["max_format_retries"] = 1
    refs = {}

    def factory(config, guard, store):
        gw = MockGateway(guard, store, scripts={"*": [CallResult("", Status.OK, text=text)]})
        refs["gw"] = gw
        return gw

    monkeypatch.setattr("framework.methods.independent.build_local_gateway", factory)
    art = run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))
    reasons = [e.payload.get("reason") for e in art.store.of_type("recovery_decision")]
    assert len(refs["gw"].calls) <= 2
    assert reasons and "INVALID_OUTPUT" in reasons[0]


def test_U03_empty_query_from_scripted_controller_also_bounded(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["", ""]}
    cfg["method"]["recovery"]["max_format_retries"] = 1
    art = run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))
    assert art.store.count("action_rejected") <= 2
    assert art.store.count("terminate") == 1


# --------------------------------------------------------------------------
# U04 ElementController 只看活跃材料
# --------------------------------------------------------------------------
def test_U04_controller_does_not_use_archived_summary(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "summary", "max_chars": 12}
    cfg["method"]["controller"] = {"type": "element"}
    task = make_task(
        elements=["甲项", "乙项", "库位"],
        scripts={
            "*": [
                {"text": "- 库位: C04\n"},
                {"text": "- 备注: ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"},
                {"text": "- 库位: D05\n"},
            ]
        },
    )
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "run"))
    queries = [e.payload["messages"][-1]["content"] for e in art.store.of_type("request_sent") if e.role == "target"]
    assert any("库位" in q for q in queries), "归档文本不能让未询问的缺项被跳过"


# --------------------------------------------------------------------------
# 停止策略
# --------------------------------------------------------------------------
def test_stop_policy_after_assembly_is_default(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["q1", "q2"]}
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg, results_root=str(tmp_path / "a"))
    assert art.termination_reason == "assembled"
    assert art.store.count("request_sent") == 2


def test_stop_policy_online_continues_until_no_progress(tmp_path, monkeypatch):
    from framework.contracts import Action, ActionType
    from framework.controller import Controller

    class AlwaysAssemble(Controller):
        name = "always_assemble"

        def propose(self, task, state, **kwargs):
            return self._mk(ActionType.ASSEMBLE, "", kwargs["node_id"], kwargs["view_id"], state.source_event_seq, "always")

    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: AlwaysAssemble())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg, results_root=str(tmp_path / "b"))
    # 同一证据包重复装配 = 无进展，必须停止而不是空转到 max_steps
    assert art.termination_reason == "no_progress_assembly"
    assert art.final_candidate is not None
    assert art.store.count("assembled") >= 1


def test_stop_policy_unknown_is_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "forever"}
    with pytest.raises(ValueError):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "c"))


def test_stop_policy_is_recorded_in_events(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04\n"}]}), config=cfg, results_root=str(tmp_path / "d"))
    started = art.store.of_type("episode_started")[0]
    assert started.payload["stopping_policy"] == "online"
