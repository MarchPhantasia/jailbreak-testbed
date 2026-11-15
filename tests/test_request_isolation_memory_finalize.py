"""本轮三项任务的针对性验证（信息隔离 / 查询记忆 / 统一收尾）。

全部使用中性短输入与离线 HTTP 替身（FakeService 拦截 urlopen），不做上下文容量测试，
不调用真实模型。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework import runner
from framework.contracts import ActionType, Status
from framework.controller import Controller

from test_execution1_r2 import FakeService, routed_cfg, run_once
from test_runner_e2e import make_cfg
from helpers import make_task

from framework.methods.independent import run_episode


class BodyCapture(FakeService):
    """记录每次 HTTP 请求的 (stage, body_json)，用于检查最终请求体。"""

    def __init__(self):
        super().__init__()
        self.bodies = []

    def __call__(self, request, *args, **kwargs):
        try:
            body = json.loads(request.data.decode("utf-8")) if request.data else {}
        except Exception:
            body = {}
        self.bodies.append(body)
        return super().__call__(request, *args, **kwargs)


def bind(monkeypatch):
    svc = BodyCapture()
    monkeypatch.setattr("urllib.request.urlopen", svc)
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-placeholder")
    return svc


def target_bodies(svc):
    """target_query 阶段的请求体（最终 HTTP body，而非中间消息数组）；排除 preflight 探针。"""
    out = []
    for b in svc.bodies:
        msgs = b.get("messages") or []
        text = " ".join(str(m.get("content", "")) for m in msgs)
        if "JBT_PREFLIGHT" in text:
            continue
        if b.get("model") == "unit-target":
            out.append(b)
    return out


def plan_bodies(svc):
    out = []
    for b in svc.bodies:
        msgs = b.get("messages") or []
        if msgs and "JBT_STAGE=PLAN" in str(msgs[0].get("content", "")):
            out.append(b)
    return out


# --------------------------------------------------------------------------
# 1) 独立请求模式：第二次 Target 请求体不含第一轮历史；固定背景保留
# --------------------------------------------------------------------------
def test_T1_isolated_target_request_body_excludes_history(tmp_path, monkeypatch):
    svc = bind(monkeypatch)
    cfg = routed_cfg(tmp_path, stage_overrides={"target_query": {"temperature": 0.0, "max_tokens": 64}})
    cfg["method"]["state"] = {"backend": "raw_history", "target_history": "isolated"}
    # 固定背景：necessary_context 由数据集提供，属于"配置明确允许的固定背景"
    plan = json.load(open(Path(runner.results_root(cfg)) / "plan.json", encoding="utf-8")) if False else None
    root, ep, plan, _ = run_once(cfg)
    tbs = target_bodies(svc)
    assert len(tbs) >= 2, "应至少发出两次 target 请求"
    first_msgs = tbs[0]["messages"]
    second_msgs = tbs[1]["messages"]
    # 结构性判定（最终 HTTP 请求体）：
    #  - 不含任何 assistant 消息（历史回复未被重放）；
    #  - 消息数 <= 3（system/固定背景/当前查询），无祖先历史；
    #  - 末条为当前查询的 user 消息。
    roles = [m.get("role") for m in second_msgs]
    assert "assistant" not in roles, "隔离模式下第二请求重放了历史回复"
    assert len(second_msgs) <= 3, f"隔离模式消息数异常：{len(second_msgs)}"
    assert second_msgs[-1]["role"] == "user"
    # 第一轮的完整回复文本不得作为任何消息出现
    first_resp_text = None
    for ev_path in [Path(root) / "episodes" / ep / "events.jsonl"]:
        for line in open(ev_path, encoding="utf-8"):
            e = json.loads(line)
            if e["type"] == "response_received" and (getattr(e, "role", "") or "") == "target":
                first_resp_text = e["payload"].get("text", "")
                break
        if first_resp_text:
            break
    second_contents = [str(m.get("content", "")) for m in second_msgs]
    if first_resp_text:
        assert first_resp_text not in second_contents, "第一轮回复文本泄漏进第二请求"
    # 消息结构：当前查询必须是最后一条；无祖先历史重放
    assert second_msgs[-1]["role"] == "user"
    assert len(second_msgs) <= 3, "隔离模式不应重放历史（system/背景/查询 至多三条）"
    # 计划冻结了策略
    assert plan["request_policy"]["target_history"] == "isolated"
    # episode_started 也记录
    ev = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "events.jsonl", encoding="utf-8")]
    started = next(e for e in ev if e["type"] == "episode_started")
    assert started["payload"]["target_history_policy"] == "isolated"


def test_T1_ancestor_mode_still_replays_history(tmp_path, monkeypatch):
    """默认 ancestor 模式行为保留：第二请求包含第一轮历史（兼容既有实验条件）。"""
    svc = bind(monkeypatch)
    cfg = routed_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history"}  # 未配置 target_history → 默认 ancestor
    root, ep, plan, _ = run_once(cfg)
    assert plan["request_policy"]["target_history"] == "ancestor"
    tbs = target_bodies(svc)
    if len(tbs) >= 2:
        q1 = tbs[0]["messages"][-1]["content"]
        all_second_text = " ".join(str(m.get("content", "")) for m in tbs[1]["messages"])
        assert q1 in all_second_text, "ancestor 模式应重放历史查询"


def test_T1_isolated_keeps_fixed_background_and_system(tmp_path, monkeypatch):
    svc = bind(monkeypatch)
    cfg = routed_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history", "target_history": "isolated"}
    # 给 Target 配置显式 system 提示（实验条件）
    cfg["target_system_prompt"] = "固定系统提示XYZ"
    root, ep, plan, _ = run_once(cfg)
    tbs = target_bodies(svc)
    assert tbs, "应有 target 请求"
    msgs = tbs[0]["messages"]
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == "固定系统提示XYZ"
    # 必要背景（固定背景）保留在最前（system 之后）
    if any("背景资料" in str(m.get("content", "")) for m in msgs):
        assert msgs[1]["role"] == "user" and "背景资料" in msgs[1]["content"]


def test_T1_unknown_target_history_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history", "target_history": "both"}
    with pytest.raises(ValueError, match="target_history"):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "x"))


# --------------------------------------------------------------------------
# 2/3) Controller 查询-结果记忆：普通回复与拒答都关联到具体查询
# --------------------------------------------------------------------------
class _Probe(Controller):
    """捕获传给 propose 的 query_history，并按序列推进。"""

    name = "probe"

    def __init__(self):
        self.captured = []
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, **kw):
        self.captured.append(query_history)
        seq = [(ActionType.ASK_TARGET, "第一条查询：查询目标库位"), (ActionType.ASK_TARGET, "第二条查询：查询登记规则"), (ActionType.STOP, "")]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, node_id, view_id, state.source_event_seq, "probe")


def test_T2_controller_memory_links_query_and_result(tmp_path, monkeypatch):
    probe = _Probe()
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: probe)
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history"}
    task = make_task(scripts={"*": [{"text": "- 库位: C04\n- 登记: 首次需要登记\n"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    assert len(probe.captured) >= 3
    second = probe.captured[1] or {}
    entries = second.get("current_branch") or []
    assert entries, "第二次规划应带查询记忆"
    e1 = entries[0]
    assert "第一条查询" in e1["query"]
    assert e1["outcome"]["status"] == "OK"
    assert e1["outcome"]["evidence_ids"], "OK 回复应关联证据 ID"
    assert e1["request_id"] and e1["call_id"] and e1["node_id"] and e1["order"] == 1
    # 查询记忆不是证据：材料里的 evidence_ids 与查询 outcome 的关联并存但来源清楚
    assert art.store.count("terminate") == 1


def test_T3_controller_memory_keeps_refused_query(tmp_path, monkeypatch):
    probe = _Probe()
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: probe)
    cfg = make_cfg(tmp_path)
    cfg["method"]["state"] = {"backend": "raw_history"}
    # 第一条查询被拒答（REFUSED），第二条正常
    task = make_task(
        scripts={"*": [{"status": "REFUSED", "text": "我不会回答这个问题。"}, {"text": "- 库位: C04\n"}]}
    )
    run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    second = probe.captured[1] or {}
    entries = second.get("current_branch") or []
    assert entries, "拒答后仍应有查询记忆"
    e1 = entries[0]
    assert "第一条查询" in e1["query"], "被拒答的具体查询必须保留"
    assert e1["outcome"]["refusal"] is True
    assert e1["outcome"]["status"] == "REFUSED"


def test_T3_transport_failure_query_is_kept(tmp_path, monkeypatch):
    probe = _Probe()
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: probe)
    cfg = make_cfg(tmp_path)
    cfg["method"]["recovery"] = {"max_network_retries": 0, "max_format_retries": 1, "total_cap": 3, "version": "r1"}
    cfg["method"]["state"] = {"backend": "raw_history"}
    task = make_task(scripts={"*": [{"status": "TRANSPORT_ERROR", "text": ""}, {"text": "- 库位: C04\n"}]})
    run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    any_link = False
    for qh in probe.captured:
        for e in (qh or {}).get("current_branch") or []:
            if "第一条查询" in e["query"] and e["outcome"]["status"] == "TRANSPORT_ERROR":
                any_link = True
    assert any_link, "传输失败的查询也应保留在记忆里"


# --------------------------------------------------------------------------
# 4) online：查询→装配→再查询→STOP 的统一收尾
# --------------------------------------------------------------------------
class _SeqOnline(Controller):
    name = "seq_online"

    def __init__(self):
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, **kw):
        seq = [
            (ActionType.ASK_TARGET, "q1"),
            (ActionType.ASSEMBLE, ""),
            (ActionType.ASK_TARGET, "q2"),
            (ActionType.STOP, ""),
        ]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, node_id, view_id, state.source_event_seq, "seq")


def test_T4_online_stop_finalizes_new_materials(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: _SeqOnline())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    task = make_task(elements=["库位", "登记"], scripts={"*": [{"text": "- 库位: C04"}, {"text": "- 登记: 首次需要登记"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    # 新材料被收尾装配进最终候选
    assert art.final_candidate is not None
    assert "首次需要登记" in art.final_candidate.answer, "STOP 后新取得材料未被收尾"
    # 旧候选保留为不可变产物
    assert len(art.engine.candidates) >= 2
    assert "首次需要登记" not in art.engine.candidates[0].answer
    # 终止记录唯一，原因为 stop_action
    assert art.store.count("terminate") == 1
    assert art.termination_reason == "stop_action"


def test_T4_after_assembly_stop_with_materials_assembles(tmp_path, monkeypatch):
    """默认 after_assembly：STOP 前若有材料且无候选，统一收尾应装配（与其他停止路径一致）。"""

    class AskThenStop(Controller):
        name = "ask_then_stop"

        def __init__(self):
            self.i = 0

        def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, **kw):
            self.i += 1
            if self.i == 1:
                return self._mk(ActionType.ASK_TARGET, "q1", node_id, view_id, state.source_event_seq)
            return self._mk(ActionType.STOP, "", node_id, view_id, state.source_event_seq)

    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: AskThenStop())
    cfg = make_cfg(tmp_path)
    task = make_task(scripts={"*": [{"text": "- 库位: C04\n"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    assert art.termination_reason == "stop_action"
    assert art.store.count("terminate") == 1
    assert art.final_candidate is not None, "有材料无候选的 STOP 应统一收尾装配"
    assert art.store.count("assembled") == 1


# --------------------------------------------------------------------------
# 5) Controller 阶段额度耗尽、Assembler 尚有额度 → 正常装配而非错误降级
# --------------------------------------------------------------------------
class _GatewayThenExhaust(Controller):
    """前 n 次 propose 真正走 Controller 网关（消耗额度）并返回 ASK；之后抛 BudgetExhausted。

    用真实网关调用消耗 plan 阶段/非 Target 总额度，保证收尾时的额度状态是真实的。
    """

    name = "gw_then_exhaust"

    def __init__(self, gateway, n_calls):
        self.gw = gateway
        self.n = n_calls
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, **kw):
        from framework.budget import BudgetExhausted
        from framework.contracts import Role

        self.i += 1
        if self.i <= self.n:
            self.gw.call(
                Role.CONTROLLER,
                "plan",
                [{"role": "user", "content": f"plan step {self.i}"}],
                {},
                node_id=node_id,
                view_id=view_id,
            )
            return self._mk(ActionType.ASK_TARGET, f"q{self.i}", node_id, view_id, state.source_event_seq)
        raise BudgetExhausted("exhausted")


def test_T5_controller_stage_exhausted_assembles_with_model(tmp_path, monkeypatch):
    svc = bind(monkeypatch)
    holder = {}

    def fake_build(kind, **kw):
        holder["ctrl"] = _GatewayThenExhaust(kw.get("gateway"), n_calls=1)
        return holder["ctrl"]

    monkeypatch.setattr("framework.methods.independent.build_controller", fake_build)
    cfg = routed_cfg(
        tmp_path,
        budget={"target_calls": 2, "non_target_calls": 8, "stage_calls": {"plan": 1, "assemble": 2}, "tokens": None, "wall_ms": None},
    )
    root, ep, plan, _ = run_once(cfg)
    ev = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "events.jsonl", encoding="utf-8")]
    term = next(e for e in ev if e["type"] == "terminate")
    assert term["payload"]["reason"] == "stage_budget_exhausted"
    # 装配走的是模型网关（assembler 阶段真实请求），不是 no_local_budget 回退
    sent = [e for e in ev if e["type"] == "request_sent" and (e.get("role") or "") == "assembler"]
    assert sent, "Controller 阶段耗尽但 Assembler 有额度时，应正常调用模型装配"
    assembled = [e for e in ev if e["type"] == "assembled"]
    assert assembled
    rejects = [json.dumps(e["payload"], ensure_ascii=False) for e in ev if e["type"] == "action_rejected"]
    assert not any("no_local_budget" in x for x in rejects), "尚有额度时不得标记 no_local_budget"


def test_T5_no_assembly_budget_reason_is_accurate(tmp_path, monkeypatch):
    """非 Target 总额度耗尽（assemble 未设单独上限）→ 回退原因按实际缺口记录。"""
    svc = bind(monkeypatch)

    def fake_build(kind, **kw):
        return _GatewayThenExhaust(kw.get("gateway"), n_calls=2)

    monkeypatch.setattr("framework.methods.independent.build_controller", fake_build)
    cfg = routed_cfg(
        tmp_path,
        budget={"target_calls": 1, "non_target_calls": 2, "stage_calls": {}, "tokens": None, "wall_ms": None},
    )
    root, ep, plan, _ = run_once(cfg)
    ev = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "events.jsonl", encoding="utf-8")]
    rejects = [json.dumps(e["payload"], ensure_ascii=False) for e in ev if e["type"] == "action_rejected"]
    assert any("no_assembly_budget" in x for x in rejects), "无装配额度应记录回退"
    # 实际缺口是非 Target 总额度（assemble 无单独上限），不得误标 assembly_stage_exhausted
    assert any("no_non_target_budget" in x or "no_local_budget" in x for x in rejects)
    assert not any("assembly_stage_exhausted" in x for x in rejects), "assemble 未设上限时不应误标阶段耗尽"
    # 原文回退保留材料，不丢候选
    cands = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "candidates.jsonl", encoding="utf-8")]
    assert cands


# --------------------------------------------------------------------------
# 6) 默认 after_assembly：材料未变不重复装配；首装即终语义不变
# --------------------------------------------------------------------------
def test_T6_after_assembly_single_assembly_and_unchanged_materials(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: _SeqOnline())
    cfg = make_cfg(tmp_path)  # 默认 after_assembly
    task = make_task(elements=["库位", "登记"], scripts={"*": [{"text": "- 库位: C04"}, {"text": "- 登记: 首次需要登记"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    # 首次装配后立即结束：后续 q2/STOP 不会执行
    assert art.termination_reason == "assembled"
    assert art.store.count("assembled") == 1
    assert art.store.count("terminate") == 1


def test_T6_online_unchanged_materials_no_reassembly_on_stop(tmp_path, monkeypatch):
    """online + STOP 且材料未变 → 不重复装配，旧候选保留。"""

    class AskAssembleStop(Controller):
        name = "aas"

        def __init__(self):
            self.i = 0

        def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, **kw):
            seq = [(ActionType.ASK_TARGET, "q1"), (ActionType.ASSEMBLE, ""), (ActionType.STOP, "")]
            action, query = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return self._mk(action, query, node_id, view_id, state.source_event_seq)

    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: AskAssembleStop())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    task = make_task(scripts={"*": [{"text": "- 库位: C04\n"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    assert art.termination_reason == "stop_action"
    assert art.store.count("assembled") == 1, "材料未变时 STOP 不应重复装配"
    assert art.store.count("terminate") == 1
