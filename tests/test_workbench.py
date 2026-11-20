"""Controller 工作台回归：关联正确性与防串绑（Issue #1 小修）。

全部使用 tmp_path 合成 episode（干净检出可运行，不依赖服务器上的 results/ 目录）。

覆盖：
- 同一视图下两次不同请求：原提议/Controller 规划输入/实际发送/Target 回复
  各自归位（request_id → generation_call_id → call_id 优先级）；
- 旧记录无 request_id 且同视图多提议 → 该项标"关联不确定"（proposal_ambiguous）；
- 同题不同运行不串绑（草稿按 run_root+episode_id 过滤）；
- 同一位置两份草稿 ID 可区分、锚点读回正确；
- 锚点 text_sha 防错位（发送内容不符拒绝保存）；
- 保存只写工作台存储，原运行文件不变。
"""
import json
import os

import pytest

from framework import webapp


def _ev(etype, **kw):
    base = {"type": etype, "call_id": "", "node_id": "root", "view_id": "",
            "role": "", "payload": {}}
    base.update(kw)
    return base


def _write_episode(root, *, task_id="sr_0000", episode_id="ep_t1", with_request_ids=True):
    """合成一个最小 episode：同一视图下两次请求（各带提议/规划调用/发送/回复）。"""
    ep_dir = os.path.join(str(root), "episodes", episode_id)
    os.makedirs(ep_dir, exist_ok=True)
    plan = {"experiment_id": "synthetic", "episodes": [
        {"episode_id": episode_id,
         "task": {"task_id": task_id, "prompt": "synthetic original task prompt"}}]}
    json.dump(plan, open(os.path.join(str(root), "plan.json"), "w", encoding="utf-8"))
    view_id = "view_shared_1"
    events = []
    for i in (1, 2):
        rid = f"req_{i:024d}" if with_request_ids else None
        gcid, tcid = f"call_ctrl_{i}", f"call_target_{i}"
        # 规划调用（controller 角色请求+响应）
        events += [
            _ev("request_sent", call_id=gcid, role="controller", view_id=view_id,
                payload={"messages": [{"role": "user", "content": f"controller input {i}"}]}),
            _ev("response_received", call_id=gcid,
                payload={"status": "OK", "text": f'{{"type":"ASK_TARGET","query":"proposal text {i}"}}'}),
        ]
        # 生成记录（提议）：request_id 与 generation_call_id
        gen_payload = {"request_id": rid, "query": f"proposal text {i}",
                       "rationale": f"rationale {i}", "strategy": "s",
                       "generation_call_id": gcid}
        events.append(_ev("request_generated", view_id=view_id, payload=gen_payload))
        # Target 实际发送（payload.request_id 可选）与回复；发送前各有一份消息准备快照
        tp = {"messages": [{"role": "user", "content": f"sent question {i}"}]}
        if rid:
            tp["request_id"] = rid
        events += [
            _ev("view_snapshot", view_id=view_id,
                payload={"messages": [{"role": "user", "content": f"prep snapshot {i}"}]}),
            _ev("request_sent", call_id=tcid, role="target", view_id=view_id, payload=tp),
            _ev("response_received", call_id=tcid,
                payload={"status": "OK", "text": f"target reply {i}"}),
        ]
    with open(os.path.join(ep_dir, "events.jsonl"), "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return str(root), episode_id


@pytest.fixture
def store(tmp_path, monkeypatch):
    fp = tmp_path / "annotations.jsonl"
    monkeypatch.setattr(webapp, "WORKBENCH_STORE", str(fp))
    return fp


@pytest.fixture
def episode(tmp_path, monkeypatch):
    """合成运行放在 tmp 的 results/ 下并 chdir，使其进入 _discover_runs 的发现范围。"""
    monkeypatch.chdir(tmp_path)
    return _write_episode(tmp_path / "results" / "run_a")


def test_same_view_two_requests_attributed_correctly(episode):
    """同视图两次请求：提议/规划输入/实际发送/回复按 request_id→call_id 各自归位。"""
    root, ep = episode
    wb = webapp.workbench_episode(root, ep)
    assert not wb.get("error")
    its = wb["interactions"]
    assert len(its) == 2
    for i, it in enumerate(its, 1):
        assert it["seq"] == i
        assert it["call_id"] == f"call_target_{i}", "第 {i} 次的发送调用错配"
        assert it["request_id"] == f"req_{i:024d}", "第 {i} 次的 request_id 错配"
        assert it["proposal"] == f"proposal text {i}", "第 {i} 次的提议错配"
        assert it["sent_text"] == f"sent question {i}", "第 {i} 次的实际发送错配"
        assert it["reply_text"] == f"target reply {i}", "第 {i} 次的回复错配"
        assert it["generation_call_id"] == f"call_ctrl_{i}"
        pc = it["planning_call"] or {}
        assert pc.get("call_id") == f"call_ctrl_{i}", "第 {i} 次的规划调用错配"
        assert pc["input_messages"][0]["content"] == f"controller input {i}", \
            "第 {i} 次的 Controller 规划输入错配"
        assert it["proposal_ambiguous"] is False
    # Controller 规划输入各自不同（同视图两次请求的核心修复点）
    assert its[0]["controller_input_messages"][0]["content"] == "controller input 1"
    assert its[1]["controller_input_messages"][0]["content"] == "controller input 2"
    # 发送前消息准备快照按事件序各归其位
    assert its[0]["target_view_snapshot"] is not None
    assert its[1]["target_view_snapshot"] is not None


def test_no_request_id_marks_ambiguous(tmp_path, monkeypatch):
    """旧记录无 request_id 且同视图多提议 → 提议/规划调用标关联不确定，其余照常。"""
    monkeypatch.chdir(tmp_path)
    root, ep = _write_episode(tmp_path / "results" / "run_old", with_request_ids=False)
    wb = webapp.workbench_episode(root, ep)
    for it in wb["interactions"]:
        assert it["proposal_ambiguous"] is True
        assert it["proposal"] is None
        assert it["planning_call"] is None
        # 发送与回复按 call_id 仍正确
        i = it["seq"]
        assert it["call_id"] == f"call_target_{i}"
        assert it["sent_text"] == f"sent question {i}"
        assert it["reply_text"] == f"target reply {i}"
        # Controller 规划输入不可唯一对应（关联不确定），但 Target 实际消息可读
        assert it["controller_input_messages"] is None


def test_same_task_different_runs_do_not_cross_bind(store, tmp_path, monkeypatch):
    """同题同 episode 名、不同运行：草稿按运行+episode 过滤，互不可见。"""
    monkeypatch.chdir(tmp_path)
    a = _write_episode(tmp_path / "results" / "run_a", task_id="sr_0000")
    b = _write_episode(tmp_path / "results" / "run_b", task_id="sr_0000")  # 同题同 episode 名
    for (root, ep) in (a, b):
        wb = webapp.workbench_episode(root, ep)
        out = webapp.workbench_save({
            "root": root, "episode_id": ep,
            "discovery": _anchor(wb, 2), "fallback": _anchor(wb, 1),
            "reason": "regression", "draft_text": f"alt {root}",
        })
        assert "saved" in out, out
    for (root, ep) in (a, b):
        rows = webapp.workbench_annotations(root=root, episode_id=ep)
        assert len(rows) == 1 and rows[0]["run_root"] == root
    assert len(webapp.workbench_annotations()) == 2


def test_same_step_two_drafts_and_sha_guard(episode, store):
    """同位双草稿 ID 可区分、锚点读回正确；text_sha 不符拒绝保存。"""
    root, ep = episode
    wb = webapp.workbench_episode(root, ep)
    body = {"root": root, "episode_id": ep,
            "discovery": _anchor(wb, 2), "fallback": _anchor(wb, 1)}
    a = webapp.workbench_save({**body, "reason": "想法一", "draft_text": "draft A"})
    b = webapp.workbench_save({**body, "reason": "想法二", "draft_text": "draft B"})
    ids = {a["saved"]["draft_id"], b["saved"]["draft_id"]}
    assert len(ids) == 2
    rows = webapp.workbench_annotations(root=root, episode_id=ep)
    assert {r["draft_id"] for r in rows} == ids
    assert {r["draft_text"] for r in rows} == {"draft A", "draft B"}
    for r in rows:
        assert r["discovery"]["seq"] == 2 and r["discovery"]["call_id"] == "call_target_2"
        assert r["fallback"]["seq"] == 1 and r["fallback"]["call_id"] == "call_target_1"
    # 防错位：text_sha 与该次实际发送不符 → 拒绝且不落盘
    bad = _anchor(wb, 1)
    bad["text_sha"] = "deadbeef00000000"
    out = webapp.workbench_save({**body, "fallback": bad, "reason": "x", "draft_text": "y"})
    assert "error" in out and "text_sha" in out["error"]
    assert len([l for l in open(store, encoding="utf-8") if l.strip()]) == 2


def test_original_run_files_untouched(episode, store):
    """保存草稿只写工作台存储，原运行文件 mtime 不变。"""
    root, ep = episode
    ep_dir = os.path.join(root, "episodes", ep)
    before = {f: os.stat(os.path.join(ep_dir, f)).st_mtime_ns for f in os.listdir(ep_dir)}
    wb = webapp.workbench_episode(root, ep)
    webapp.workbench_save({
        "root": root, "episode_id": ep,
        "discovery": _anchor(wb, 2), "fallback": _anchor(wb, 1),
        "reason": "r", "draft_text": "d",
    })
    after = {f: os.stat(os.path.join(ep_dir, f)).st_mtime_ns for f in os.listdir(ep_dir)}
    assert before == after, "原运行文件被改动"
    assert store.exists()


def _anchor(wb, seq):
    it = wb["interactions"][seq - 1]
    return {"seq": it["seq"], "call_id": it["call_id"], "text_sha": it["text_sha"]}
