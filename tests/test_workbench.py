"""Controller 工作台回归：草稿锚定与防串绑（Issue #1）。

覆盖：
- 同题不同运行不串绑（R22=mainflow8 与 R12=mainflow7 同为 sr_0000、episode 同名）；
- 同一运行内不同调用锚点各自可找回、草稿 ID 可区分；
- 锚点 text_sha 防错位（发送内容不符时拒绝保存）；
- 原运行文件不被写入（工作台只写自己的存储）。
"""
import json
import os

import pytest

from framework import webapp


@pytest.fixture
def store(tmp_path, monkeypatch):
    fp = tmp_path / "annotations.jsonl"
    monkeypatch.setattr(webapp, "WORKBENCH_STORE", str(fp))
    return fp


def _anchor(wb, seq):
    it = wb["interactions"][seq - 1]
    return {"seq": it["seq"], "call_id": it["call_id"], "text_sha": it["text_sha"]}


def test_same_task_different_runs_do_not_cross_bind(store):
    """R22（mainflow8）与 R12（mainflow7）同题同 episode 名：草稿按运行+episode 过滤。"""
    r22 = (webapp.workbench_episode("results/mainflow8_sr_batch2_20261117_r1", "ep_49f441c969"),)
    r12 = webapp.workbench_episode("results/mainflow7_sr_batch1_20261117_r1", "ep_49f441c969")
    assert r22[0]["task"]["task_id"] == r12["task"]["task_id"] == "sr_0000"
    for wb, root in ((r22[0], "results/mainflow8_sr_batch2_20261117_r1"),
                     (r12, "results/mainflow7_sr_batch1_20261117_r1")):
        out = webapp.workbench_save({
            "root": root, "episode_id": "ep_49f441c969",
            "discovery": _anchor(wb, 5), "fallback": _anchor(wb, 2),
            "reason": "regression", "draft_text": "alt question",
        })
        assert "saved" in out, out
    for root, want in (("results/mainflow8_sr_batch2_20261117_r1", "mainflow8"),
                       ("results/mainflow7_sr_batch1_20261117_r1", "mainflow7")):
        rows = webapp.workbench_annotations(root=root, episode_id="ep_49f441c969")
        assert len(rows) == 1, f"{want} 应只看到自己的 1 份，实际 {len(rows)}"
        assert rows[0]["run_root"] == root
    # 不过滤时两份都在
    assert len(webapp.workbench_annotations()) == 2


def test_same_step_two_drafts_both_recoverable(store):
    wb = webapp.workbench_episode("results/mainflow8_sr_batch2_20261117_r1", "ep_49f441c969")
    body = {"root": "results/mainflow8_sr_batch2_20261117_r1", "episode_id": "ep_49f441c969",
            "discovery": _anchor(wb, 5), "fallback": _anchor(wb, 2)}
    a = webapp.workbench_save({**body, "reason": "想法一", "draft_text": "draft A"})
    b = webapp.workbench_save({**body, "reason": "想法二", "draft_text": "draft B"})
    ids = {a["saved"]["draft_id"], b["saved"]["draft_id"]}
    assert len(ids) == 2, "同一位置的两份草稿必须有可区分的 ID"
    rows = webapp.workbench_annotations(root=body["root"], episode_id=body["episode_id"])
    assert {r["draft_id"] for r in rows} == ids
    texts = {r["draft_text"] for r in rows}
    assert texts == {"draft A", "draft B"}
    # 锚点读回正确
    for r in rows:
        assert r["discovery"]["seq"] == 5 and r["fallback"]["seq"] == 2
        assert r["discovery"]["call_id"] == wb["interactions"][4]["call_id"]
        assert r["fallback"]["call_id"] == wb["interactions"][1]["call_id"]


def test_wrong_text_sha_rejected(store):
    """锚点 text_sha 与该次发送不符 → 拒绝（防错位保存）。"""
    wb = webbench = webapp.workbench_episode("results/mainflow8_sr_batch2_20261117_r1", "ep_49f441c969")
    bad = _anchor(wb, 2)
    bad["text_sha"] = "deadbeef00000000"
    out = webapp.workbench_save({
        "root": "results/mainflow8_sr_batch2_20261117_r1", "episode_id": "ep_49f441c969",
        "discovery": _anchor(wb, 5), "fallback": bad,
        "reason": "x", "draft_text": "y",
    })
    assert "error" in out and "text_sha" in out["error"]
    assert not os.path.exists(store) or not [l for l in open(store, encoding="utf-8") if l.strip()]


def test_original_run_files_untouched(store):
    """保存草稿不写原运行目录（只写工作台存储）。"""
    ep_dir = "results/mainflow8_sr_batch2_20261117_r1/episodes/ep_49f441c969"
    before = {f: os.stat(os.path.join(ep_dir, f)).st_mtime_ns
              for f in os.listdir(ep_dir)}
    wb = webapp.workbench_episode("results/mainflow8_sr_batch2_20261117_r1", "ep_49f441c969")
    webapp.workbench_save({
        "root": "results/mainflow8_sr_batch2_20261117_r1", "episode_id": "ep_49f441c969",
        "discovery": _anchor(wb, 3), "fallback": _anchor(wb, 1),
        "reason": "r", "draft_text": "d",
    })
    after = {f: os.stat(os.path.join(ep_dir, f)).st_mtime_ns for f in os.listdir(ep_dir)}
    assert before == after, "原运行文件被改动"
    assert store.exists(), "工作台存储应写入"
