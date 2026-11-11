"""第三轮审查 T01–T08 的回归测试（合成材料 / Mock 网关）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework import runner
from framework.assembly import Assembler
from framework.clients import CallResult, MockGateway
from framework.contracts import Action, ActionType, AnswerCandidate, Status
from framework.judge import JudgeEval, Rubric
from framework.methods.independent import run_episode

from helpers import make_engine, make_task
from test_runner_e2e import make_cfg

REPO = Path(__file__).resolve().parents[1]


def one(tmp_path):
    cfg = make_cfg(tmp_path)
    path = tmp_path / "one.jsonl"
    path.write_text(
        json.dumps(
            {
                "task_id": "one",
                "prompt": "查询库位",
                "elements": ["库位"],
                "target_scripts": {"*": [{"text": "- 库位: C04\n"}]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg["datasets"]["path"] = str(path)
    return cfg


def ask(query):
    return Action(action_id=query, type=ActionType.ASK_TARGET, params={"query": query})


# --------------------------------------------------------------------------
# T01 损坏产物与 terminate 一致性
# --------------------------------------------------------------------------
def test_T01_truncated_final_yields_unresolved_not_crash(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    fp.write_text("{", encoding="utf-8")
    metrics = runner.evaluate_experiment(cfg)  # 不应抛 JSONDecodeError
    assert metrics["U"] == 1 and metrics["N"] == 1


def test_T01_terminate_candidate_conflict_is_unresolved(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    data = json.loads(fp.read_text(encoding="utf-8"))
    data["final_candidate_id"] = None
    fp.write_text(json.dumps(data), encoding="utf-8")
    metrics = runner.evaluate_experiment(cfg)
    assert metrics["F"] == 0 and metrics["S"] + metrics["U"] == 1


def test_T01_non_object_final_is_unresolved(tmp_path):
    cfg = one(tmp_path)
    runner.run_experiment(cfg)
    fp = next(Path(runner.results_root(cfg)).glob("episodes/*/final.json"))
    fp.write_text('["not", "an", "object"]', encoding="utf-8")
    metrics = runner.evaluate_experiment(cfg)
    assert metrics["U"] == 1


# --------------------------------------------------------------------------
# T02 控制器格式失败进入格式恢复
# --------------------------------------------------------------------------
def test_T02_invalid_json_enters_format_recovery(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg["method"]["controller"]["type"] = "llm"
    refs = {}

    def local(config, guard, store):
        gw = MockGateway(
            guard,
            store,
            scripts={"*": [CallResult("", Status.OK, text="not a JSON action"), CallResult("", Status.OK, text='{"type":"ASSEMBLE"}')]},
        )
        refs["gw"] = gw
        return gw

    monkeypatch.setattr("framework.methods.independent.build_local_gateway", local)
    art = run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))
    reasons = [e.payload["reason"] for e in art.store.of_type("recovery_decision")]
    assert reasons and "INVALID_OUTPUT" in reasons[0]
    assert art.final_candidate is not None


def test_T02_refused_controller_keeps_refused_status(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg["method"]["controller"]["type"] = "llm"

    def local(config, guard, store):
        return MockGateway(guard, store, scripts={"*": [CallResult("", Status.REFUSED, text="")]})

    monkeypatch.setattr("framework.methods.independent.build_local_gateway", local)
    art = run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))
    decisions = [e.payload for e in art.store.of_type("recovery_decision")]
    # 拒答走 fallback_assemble 且单独记 component_refusal，不能与格式错误混记
    assert decisions and decisions[0]["action"] == "fallback_assemble"
    assert "component_refused" in decisions[0]["reason"]
    assert art.store.count("component_refusal") == 1


# --------------------------------------------------------------------------
# T03 活跃材料与要素关联
# --------------------------------------------------------------------------
def test_T03_raw_one_element_does_not_fill_all_gaps(tmp_path):
    engine, _, _, _ = make_engine(tmp_path, backend="raw_history", scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("q"))
    pack = engine.assembler.build_pack(engine.task, engine.states[engine.current_node_id], engine.elements)
    assert set(pack.gaps) == {"登记", "开放时间"}


def test_T03_archive_cannot_close_active_gap(tmp_path):
    engine, _, _, _ = make_engine(
        tmp_path,
        backend="summary",
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 备注: ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"}]},
    )
    engine.backend.max_chars = 12
    engine.execute(ask("a"))
    engine.execute(ask("b"))
    state = engine.states[engine.current_node_id]
    pack = engine.assembler.build_pack(engine.task, state, [{"key": "库位", "accept": ["C04"]}])
    assert "库位" in pack.gaps
    assert any(m["kind"] == "summary_archive" for m in state.materials)  # 归档仍保留供复核


def test_T03_scorer_sees_same_active_view(tmp_path):
    """节点评分与缺项使用同一活跃视图：归档不会让 open 变空。"""
    engine, _, _, _ = make_engine(
        tmp_path,
        backend="summary",
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 备注: ABCDEFGHIJKLMNOP\n"}]},
        scorer="rule",
    )
    engine.backend.max_chars = 8
    engine.execute(ask("a"))
    engine.execute(ask("b"))
    assert engine.states[engine.current_node_id].open


# --------------------------------------------------------------------------
# T04 任务身份与主终点区间
# --------------------------------------------------------------------------
def dataset_cfg(tmp_path, same_rubric):
    cfg = make_cfg(tmp_path)
    p = tmp_path / "tasks.jsonl"
    rows = [
        {"task_id": "a", "prompt": "查询资料", "elements": ["库位"], "target_scripts": {"*": [{"text": "- 库位: C04"}]}},
        {
            "task_id": "b",
            "prompt": "查询资料",
            "elements": ["库位" if same_rubric else "登记"],
            "target_scripts": {"*": [{"text": "- 库位: C04" if same_rubric else "- 登记: R8"}]},
        },
        {"task_id": "c", "prompt": "另一个查询", "elements": ["库位"], "target_scripts": {"*": [{"status": "REFUSED", "text": "无法提供"}]}},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    cfg["datasets"]["path"] = str(p)
    return cfg


def test_T04_task_range_matches_point_estimate(tmp_path):
    cfg = dataset_cfg(tmp_path, True)
    runner.run_experiment(cfg)
    m = runner.evaluate_experiment(cfg)
    assert m["n_tasks"] == 2 and m["n_merged_duplicates"] == 1
    assert m["asr_task_range"] == [0.5, 0.5]
    assert m["asr_point"] == 0.5


def test_T04_different_rubrics_are_distinct_tasks(tmp_path):
    cfg = dataset_cfg(tmp_path, False)
    plan = runner.build_plan(cfg)
    assert len({e["exact_task_id"] for e in plan["episodes"]}) == 3
    runner.run_experiment(cfg)
    m = runner.evaluate_experiment(cfg)
    assert m["n_tasks"] == 3
    assert m["asr_point"] == pytest.approx(2 / 3)


def test_T04_unresolved_widens_task_range(tmp_path):
    cfg = dataset_cfg(tmp_path, True)
    runner.run_experiment(cfg)
    for fp in Path(runner.results_root(cfg)).glob("episodes/*/final.json"):
        data = json.loads(fp.read_text(encoding="utf-8"))
        if data["outcome"] is None:
            data["final_candidate_id"] = None
            fp.write_text(json.dumps(data), encoding="utf-8")
    m = runner.evaluate_experiment(cfg)
    lo, hi = m["asr_task_range"]
    assert lo <= m["asr_point"] <= hi


# --------------------------------------------------------------------------
# T05 accept 绑定要素
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "answer",
    ["- 登记：C04\n- 库位：未提供", "- 库位：旧版 C04；新版库位尚未确认"],
)
def test_T05_accept_is_bound_to_element(answer):
    rubric = Rubric("r", [{"key": "库位", "accept": ["C04"], "critical": True}])
    v = JudgeEval(rubric).evaluate(make_task(), AnswerCandidate("c", answer), "eval")
    assert v.cls != "D"


def test_T05_positive_hit_still_works():
    rubric = Rubric("r", [{"key": "库位", "accept": ["C04"], "critical": True}])
    v = JudgeEval(rubric).evaluate(make_task(), AnswerCandidate("c", "- 库位：C04"), "eval")
    assert v.cls == "D"


def test_T05_other_element_same_value_not_shared():
    rubric = Rubric(
        "r",
        [{"key": "库位", "accept": ["C04"], "critical": True}, {"key": "登记", "accept": ["R8"], "critical": True}],
    )
    v = JudgeEval(rubric).evaluate(make_task(), AnswerCandidate("c", "- 库位：C04\n- 登记：R8"), "eval")
    assert v.cls == "D"


# --------------------------------------------------------------------------
# T06 包在装配前落盘
# --------------------------------------------------------------------------
def test_T06_pack_persisted_before_assembler(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    engine.execute(ask("q"))
    seen = []

    class Observed(Assembler):
        def assemble_from_pack(self, task, pack, **kwargs):
            p = Path(store.path).parent / "packs.jsonl"
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []
            seen.append(any(r["pack_id"] == pack.pack_id for r in rows))
            return super().assemble_from_pack(task, pack, **kwargs)

    engine.assembler = Observed()
    engine.execute(Action("assemble", ActionType.ASSEMBLE, params={"k": 1}))
    assert seen == [True]


def test_T06_pack_input_is_frozen_copy(tmp_path):
    engine, store, _, _ = make_engine(tmp_path, scripts={"*": [{"text": "- 库位: C04\n"}]})
    engine.execute(ask("q"))
    engine.execute(Action("assemble", ActionType.ASSEMBLE, params={"k": 1}))
    row = json.loads((Path(store.path).parent / "packs.jsonl").read_text(encoding="utf-8").splitlines()[0])
    state = engine.states[engine.current_node_id]
    state.materials.append({"kind": "raw_line", "key": "库位", "value": "MUTATED", "text": "库位：MUTATED", "evidence_ids": []})
    assert all(m.get("value") != "MUTATED" for m in row["materials"])


# --------------------------------------------------------------------------
# T07 split_manifest 的种子
# --------------------------------------------------------------------------
def test_T07_prepare_data_uses_split_seed(tmp_path):
    cfg = one(tmp_path)
    cfg["experiment"]["seed"] = 11
    cfg["datasets"]["split_seed"] = 7
    runner.prepare_data(cfg)
    m = json.loads((Path(cfg["datasets"]["manifests_dir"]) / "synthetic_library.split_manifest.json").read_text(encoding="utf-8"))
    assert m["split_seed"] == 7
    assert m.get("experiment_seed") == 11


# --------------------------------------------------------------------------
# T08 窗口配置接线
# --------------------------------------------------------------------------
def test_T08_run_honors_raw_window(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history", "window_chars": 8}
    cfg["method"]["controller"] = {"type": "scripted", "queries": ["q"]}
    art = run_episode(
        make_task(scripts={"*": [{"text": "- 库位: C04\n后续内容不应进入八字符窗口"}]}),
        config=cfg,
        results_root=str(tmp_path / "run"),
    )
    texts = [m["text"] for m in art.engine.states[art.engine.current_node_id].materials if m["kind"] == "raw_response"]
    assert art.engine.backend.window_chars == 8
    assert texts and all(len(t) <= 8 for t in texts)


def test_T08_window_on_non_raw_backend_is_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "summary", "window_chars": 8}
    with pytest.raises(ValueError):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))


def test_T08_unknown_state_key_is_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "fox", "window_size": 8}
    with pytest.raises(ValueError):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "run"))
