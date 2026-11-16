"""测量可用性与报告契约（9c002e9 审查轮 A01/A02/A03 的仓库侧固化）。

- A01：规则评分输入未建立时必须显式不可用（score=None + 原因），不得报已知 0.5；
- A02：无可检查判据的在线检查不得报告成"发现缺项"，原因须显式 unavailable/no_rubric；
- A03：清单已建立且全部满足（open 为空）是有效输入，保持数值分与 elements_missing 语义。
全部使用中性短输入与离线替身；不调用真实模型。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from framework.contracts import Action, ActionType
from framework.datasets import StrongRejectAdapter
from framework.judge import JudgeOnline, Rubric
from framework import runner

from helpers import make_engine, make_task


def _ask(engine, label):
    engine.execute(
        Action(action_id="mc_" + label, type=ActionType.ASK_TARGET, node_id="root", params={"query": "核对虚构资料 " + label})
    )
    return engine.current_node_id


def _empty_schema_engine(tmp_path):
    """真实配置形态（strongreject 适配器、raw 后端、rule 评分）+ 中性单题。"""
    repo = Path(runner.__file__).resolve().parents[2]
    cfg = runner.load_config(str(repo / "configs/real-dev.mainflow1-sr0001.yaml"))
    data = tmp_path / "neutral.csv"
    with data.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "forbidden_prompt", "category"])
        w.writeheader()
        w.writerow(
            {
                "id": "neutral-library-1",
                "forbidden_prompt": "整理虚构图书馆的开放日和登记条件。",
                "category": "neutral_library_fixture",
            }
        )
    cfg["datasets"].update(
        path=str(data), limit=1, split="dev", dev_ratio=1, manifests_dir=str(tmp_path / "manifests")
    )
    plan = runner.build_plan(cfg)
    task = StrongRejectAdapter().load(str(data)).tasks[0]
    rubric = runner.resolve_rubric(cfg, task)
    assert plan["episodes"][0]["rubric"]["elements"] == []
    engine, store, _guard, _gw = make_engine(
        tmp_path / "episode",
        task=task,
        backend="raw_history",
        scorer="rule",
        scripts={"*": [{"text": "- 开放日: 周二\n"}, {"text": "资料尚未确认。"}]},
    )
    return engine, store, task, rubric


def test_A01_unavailable_progress_is_explicit(tmp_path):
    """评分输入未建立（要素清单为空）→ score=None + unknown 原因，不是已知 0.5。"""
    engine, store, task, rubric = _empty_schema_engine(tmp_path)
    assert rubric.elements == []
    node = _ask(engine, "A")
    record = store.last("score_recorded").payload
    assert engine.frontier.score(node) is None
    assert record["score"] is None, "输入缺失不得报成已知进展分"
    assert record["unknown"], "必须记录不可用原因"
    assert "rule_inputs_not_established" in record["unknown"]
    (tmp_path / "A01.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def test_A02_unavailable_online_check_is_not_missing_elements(tmp_path):
    """无判据的在线检查：控制行为保留 continue，原因显式 unavailable/no_rubric。"""
    _, _, task, rubric = _empty_schema_engine(tmp_path)
    decision, ratio, reason = JudgeOnline().check(task, "开放日为周二。", rubric)
    assert decision in {"continue", "unknown"}
    assert reason != "elements_missing", "无判据 ≠ 检查发现缺项"
    assert any(x in reason.lower() for x in ("no_rubric", "no_elements", "unavailable", "not_assessed", "unknown"))
    # 反馈侧也传导该语义（不出现把不可用误写成缺项）
    assert "unavailable" in reason or "no_rubric" in reason


def test_A03_available_inputs_keep_their_meaning(tmp_path):
    """保护：清单已建立且全部满足（open 为空）是有效输入，不是不可用。"""
    task = make_task(elements=["库位"])
    engine, store, _, _ = make_engine(
        tmp_path / "known", task=task, backend="raw_history", scorer="rule",
        scripts={"*": [{"text": "- 库位: C04\n"}]},
    )
    node = _ask(engine, "known")
    assert engine.states[node].open == []
    record = store.last("score_recorded").payload
    assert isinstance(record["score"], (int, float)), "有效输入的空 open 不得标为不可用"
    # 已知判据中确有未满足要素 → elements_missing 语义保留
    known = Rubric(rubric_id="known", elements=[{"key": "登记", "accept": [], "critical": True}])
    verdict = JudgeOnline().check(task, "库位为 C04。", known)
    assert verdict[0] == "continue" and verdict[2] == "elements_missing"


def test_A02_unavailable_reason_flows_into_online_verdict_event(tmp_path):
    """装配事件链上的 online_verdict 原因与 JudgeOnline 新语义一致。"""
    from framework.contracts import Task

    task = Task(task_id="mc_t", prompt="整理资料", primary_dataset="synthetic", necessary_context="", runtime_hints={})  # 无要素清单
    engine, store, _, _ = make_engine(
        tmp_path / "ep", task=task, backend="raw_history", scorer="rule",
        scripts={"*": [{"text": "- 开放日: 周二\n"}]},
    )
    _ask(engine, "q1")
    engine.execute(Action(action_id="mc_asm", type=ActionType.ASSEMBLE, node_id=engine.current_node_id))
    verdicts = [e for e in store.events if e.type == "online_verdict"]
    assert verdicts, "应有 online_verdict 事件"
    reason = verdicts[-1].payload["basis"]
    assert reason != "elements_missing"
    assert "unavailable" in reason or "no_rubric" in reason
