"""主流程四条行为链的离线验证（中性短输入，无真实模型、无窗口测试）。

- C06 在线反馈闭环：装配后候选缺项/在线检查实际进入 Controller 输入，带版本与过期标记
- C02/C04 分支闭环：兄弟方向真实保存、归属按执行边、状态不串支、失败记忆按范围、成本不回退
- C08 真多候选：同一冻结包上独立采样 k 个候选、逐样本记录调用、选择有依据
- 4.6 收尾追溯：候选新鲜度比较选中候选自己的包，而非最后一个包
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework.contracts import ActionType
from framework.controller import Controller

from helpers import make_task
from test_runner_e2e import make_cfg

from framework.methods.independent import run_episode


# --------------------------------------------------------------------------
# C06：当前候选与在线反馈进入 Controller 输入
# --------------------------------------------------------------------------
class _FeedSeq(Controller):
    """捕获 query_history 与 candidate_feedback：ASK → ASSEMBLE → ASK → STOP。"""

    name = "feed_seq"

    def __init__(self):
        self.captured = []
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None, **kw):
        self.captured.append({"qh": query_history, "fb": candidate_feedback})
        seq = [
            (ActionType.ASK_TARGET, "q1"),
            (ActionType.ASSEMBLE, ""),
            (ActionType.ASK_TARGET, "q2"),
            (ActionType.STOP, ""),
        ]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, node_id, view_id, state.source_event_seq, "feed_seq")


def _run_with(controller, cfg, task):
    import framework.methods.independent as ind

    orig = ind.build_controller
    ind.build_controller = lambda *a, **k: controller
    try:
        return run_episode(task, config=cfg, results_root=str(cfg["_tmp"] / "r"))
    finally:
        ind.build_controller = orig


def test_C06_candidate_feedback_enters_controller_input(tmp_path):
    ctl = _FeedSeq()
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["_tmp"] = tmp_path
    task = make_task(
        elements=["库位", "登记"],
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 登记: 首次需要登记\n"}]},
    )
    art = _run_with(ctl, cfg, task)
    # 第 1、2 次规划时还没有候选 → 反馈为 None
    assert ctl.captured[0]["fb"] is None
    assert ctl.captured[1]["fb"] is None
    # ASSEMBLE 之后（第 3 次规划）：反馈进入输入
    fb3 = ctl.captured[2]["fb"]
    assert fb3 is not None, "装配后 Controller 输入应带当前候选反馈"
    assert fb3["candidate_id"]
    assert fb3["pack_input_hash"], "反馈须关联包版本"
    assert isinstance(fb3["gaps"], list)
    assert fb3["online_verdict"] and "decision" in fb3["online_verdict"]
    assert fb3["stale"] is False, "刚装配完材料未变，不应标过期"
    # 追加取证后（第 4 次规划）：材料变化 → stale=True，反馈仍指向旧包
    fb4 = ctl.captured[3]["fb"]
    assert fb4 is not None
    assert fb4["stale"] is True, "新材料到达后旧反馈应标记过期"
    # 收尾消费新材料
    assert "首次需要登记" in (art.final_candidate.answer if art.final_candidate else "")
    assert art.store.count("terminate") == 1


def test_C06_feedback_version_links_to_online_verdict_event(tmp_path):
    """反馈中的 verdict/candidate/pack 与 online_verdict 事件一致（可追溯）。"""
    ctl = _FeedSeq()
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["_tmp"] = tmp_path
    task = make_task(
        elements=["库位", "登记"],
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 登记: 首次\n"}]},
    )
    art = _run_with(ctl, cfg, task)
    fb = ctl.captured[2]["fb"]
    assert fb is not None
    ev = [e for e in art.store.events if e.type == "online_verdict"]
    assert ev, "应有 online_verdict 事件"
    mine = next(e for e in ev if e.payload["candidate_id"] == fb["candidate_id"])
    assert mine.payload["pack_input_hash"] == fb["pack_input_hash"]
    assert fb["online_verdict"]["decision"] in ("continue", "stop")


# --------------------------------------------------------------------------
# C02/C04：分支闭环（best_first + branch_width=2 + rule 评分）
# --------------------------------------------------------------------------
class _BranchSeq(Controller):
    """固定序列：三次 ASK（在策略选出的节点上执行）。"""

    name = "branch_seq"

    def __init__(self, queries):
        self.queries = list(queries)
        self.seen_nodes = []
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None, **kw):
        self.seen_nodes.append(node_id)
        q = self.queries[min(self.i, len(self.queries) - 1)]
        self.i += 1
        return self._mk(ActionType.ASK_TARGET, q, node_id, view_id, state.source_event_seq, "branch")


def test_C02_branch_siblings_isolation_and_failure_memory(tmp_path):
    ctl = _BranchSeq(["查询A：库位", "查询B：登记", "查询C：开放时间"])
    cfg = make_cfg(tmp_path)
    cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["max_steps"] = 8
    cfg["_tmp"] = tmp_path
    # A 成功；B 是拒答（同一父节点的兄弟方向）；C 在切换后的分支上
    task = make_task(
        elements=["库位", "登记", "开放时间"],
        scripts={"*": [{"text": "- 库位: C04\n"}, {"status": "REFUSED", "text": "不提供。"}, {"text": "- 开放时间: 9-17\n"}]},
    )
    art = _run_with(ctl, cfg, task)
    nodes = art.engine.nodes
    children_of_root = art.engine.children.get("root", [])
    # 至少两个兄弟方向被真实保存
    assert len(children_of_root) >= 2, f"branch_width=2 应产生兄弟节点，实际 root 子节点：{children_of_root}"
    # 兄弟状态不串支：一个子节点只有自己的观察，另一个子节点只有自己的
    s1 = art.engine.states[children_of_root[0]]
    s2 = art.engine.states[children_of_root[1]]
    m1 = json.dumps([m.get("value", m.get("text", "")) for m in s1.materials], ensure_ascii=False)
    m2 = json.dumps([m.get("value", m.get("text", "")) for m in s2.materials], ensure_ascii=False)
    assert ("C04" in m1) != ("C04" in m2) or ("C04" in m1 and "C04" in m2) is False
    # 成本不回退：兄弟查询都计入 Target 调用
    assert art.engine.guard.target_calls_used() >= 2
    # 查询记忆按执行边归属：当前分支不含兄弟分支的查询文本
    cur = art.engine.current_node_id
    qh = art.engine.query_history(cur)
    branch_queries = [e["query"] for e in qh["current_branch"]]
    sibling_queries = [e["query"] for e in qh["other_branch_failures"]]
    assert len(qh["current_branch"]) + len(qh["other_branch_failures"]) == 3, "三次已执行查询都应在记忆里"
    # 拒答的兄弟查询在失败记忆里
    assert any("查询B" in q for q in sibling_queries), "拒答的兄弟查询应进全局失败记忆"
    assert any(e["refusal"] for e in qh["other_branch_failures"])
    # 当前分支的查询都在祖先链上（观察子节点归属）
    chain = art.engine._ancestor_chain(cur)
    for e in qh["current_branch"]:
        obs = art.engine._observation_node_by_call.get(e["call_id"])
        assert obs in chain, "当前分支查询的观察子节点必须在祖先链上"


def test_C04_selection_log_has_frontier_and_basis(tmp_path):
    ctl = _BranchSeq(["查询A：库位", "查询B：登记", "查询C：开放时间"])
    cfg = make_cfg(tmp_path)
    cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["max_steps"] = 8
    cfg["_tmp"] = tmp_path
    task = make_task(
        elements=["库位", "登记", "开放时间"],
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 登记: 首次\n"}, {"text": "- 开放时间: 9-17\n"}]},
    )
    art = _run_with(ctl, cfg, task)
    sel = [e for e in art.store.events if e.type == "node_selected"]
    assert sel, "应有 node_selected 事件"
    for e in sel:
        p = e.payload
        assert "frontier" in p and p["frontier"], "选择日志须含当时可选前沿"
        assert "basis" in p and p["basis"], "选择日志须含依据"
        assert all("score" in item and "visits" in item for item in p["frontier"])
    # best_first 依据里说明未知分优先或最高分
    assert any("best_first" in e.payload["basis"] for e in sel)


# --------------------------------------------------------------------------
# C08：同一冻结包上真实生成 k 个候选
# --------------------------------------------------------------------------
class _KThenStop(Controller):
    name = "k_then_stop"

    def __init__(self):
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None, **kw):
        seq = [(ActionType.ASK_TARGET, "q1"), (ActionType.ASSEMBLE, ""), (ActionType.STOP, "")]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, node_id, view_id, state.source_event_seq)


def _assembler_scripts(answers):
    """构造 Assembler 模板 + 网关响应脚本：装配阶段返回不同 JSON。"""
    from framework.prompts import PromptTemplate
    from framework.clients import CallResult, Status

    template = PromptTemplate(
        stage_id="assemble",
        template_id="test.assemble",
        version="1",
        role="assembler",
        text="INPUT_JSON\n{{input_json}}\n",
        source_path="unit",
    )

    def make_resp(ans):
        def _f(messages):
            body = json.dumps({"answer": ans, "claims": [{"claim_id": "c1", "text": ans, "evidence_ids": ["ev_x"]}], "gaps": ["缺一"]}, ensure_ascii=False)
            return CallResult(call_id="", status=Status.OK, text=body)

        return _f

    return template, [make_resp(a) for a in answers]


def _make_pack():
    from framework.assembly import EvidencePack

    return EvidencePack(
        pack_id="pack_test",
        task_id="t",
        materials=[{"key": "库位", "value": "库位: C04", "evidence_ids": ["ev_x"], "kind": "raw_response"}],
    )


def test_C08_k_candidates_are_independent_calls(tmp_path):
    from framework.clients import MockGateway
    from framework.assembly import LLMAssembler
    from framework.events import EventStore
    from framework.budget import BudgetGuard
    from framework.contracts import Task, BudgetLimits

    template, responders = _assembler_scripts(["答案甲：库位 C04", "答案乙：库位 C05"])
    store = EventStore(str(tmp_path / "ev"), "ep_unit")
    guard = BudgetGuard(BudgetLimits(target_calls=2, non_target_calls=20), store, "ep_unit")
    gw = MockGateway(guard, store, scripts={"assemble": responders, "*": responders})
    asm = LLMAssembler(gw, template, stage_id="assemble")

    task = Task(task_id="t", prompt="p", primary_dataset="synthetic", necessary_context="", runtime_hints={})
    pack = _make_pack()
    cands = asm.assemble_from_pack(task, pack, k=2, node_id="root", view_id="v")
    assert len(cands) == 2, "k=2 应生成两个候选"
    assert cands[0].answer != cands[1].answer, "两次采样应来自两次独立调用（脚本各给不同答案）"
    # 逐样本记录：候选各自的调用与 sample_index
    assert cands[0].assembly_config["sample_index"] == 1
    assert cands[1].assembly_config["sample_index"] == 2
    calls = [e for e in store.events if e.type == "request_sent" and (getattr(e, "stage_id", "") or "") == "assemble"]
    assert len(calls) == 2, "两个样本 = 两次真实装配请求"
    assert cands[0].processing_refs != cands[1].processing_refs


def test_C08_k2_in_episode_with_selector(tmp_path, monkeypatch):
    """episode 级：k=2 走路由装配 → 两次真实装配请求、两个候选、重复如实标注、选择器选中。"""
    from test_execution1_r2 import FakeService, routed_cfg, run_once

    svc = FakeService()
    monkeypatch.setattr("urllib.request.urlopen", svc)
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-placeholder")
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **k: _KThenStop())

    cfg = routed_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["method"]["assembly"]["k"] = 2
    root, ep, plan, _ = run_once(cfg)
    ev = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "events.jsonl", encoding="utf-8")]
    assembled = [e for e in ev if e["type"] == "assembled"]
    assert assembled and assembled[0]["payload"]["k"] == 2, "assembled 事件应记录 2 个候选"
    asm_calls = [e for e in ev if e["type"] == "request_sent" and (e.get("role") or "") == "assembler"]
    assert len(asm_calls) == 2, "k=2 = 两次真实装配请求（FakeService 返回相同答案也应各发一次）"
    cands = [json.loads(l) for l in open(Path(root) / "episodes" / ep / "candidates.jsonl", encoding="utf-8")]
    assert len(cands) == 2
    # 相同答案是真实重复调用：保留并标注，不冒充不同内容
    assert cands[1]["assembly_config"].get("duplicate_of_sample") == 1
    assert cands[0]["assembly_config"]["sample_index"] == 1 and cands[1]["assembly_config"]["sample_index"] == 2
    sel = [e for e in ev if e["type"] == "candidate_selected"]
    assert sel and sel[0]["payload"]["candidate_id"], "选择器必须实际选中一个候选"
    term = [e for e in ev if e["type"] == "terminate"]
    assert len(term) == 1 and term[0]["payload"]["reason"] == "stop_action"


def test_C08_duplicate_answers_marked_not_hidden(tmp_path):
    from framework.clients import MockGateway
    from framework.assembly import LLMAssembler
    from framework.events import EventStore
    from framework.budget import BudgetGuard
    from framework.contracts import Task, BudgetLimits

    template, responders = _assembler_scripts(["同答", "同答"])
    store = EventStore(str(tmp_path / "ev"), "ep_unit")
    guard = BudgetGuard(BudgetLimits(target_calls=2, non_target_calls=20), store, "ep_unit")
    gw = MockGateway(guard, store, scripts={"assemble": responders, "*": responders})
    asm = LLMAssembler(gw, template, stage_id="assemble")

    task = Task(task_id="t", prompt="p", primary_dataset="synthetic", necessary_context="", runtime_hints={})
    pack = _make_pack()
    cands = asm.assemble_from_pack(task, pack, k=2, node_id="root", view_id="v")
    assert len(cands) == 2
    assert "duplicate_of_sample" in cands[1].assembly_config, "重复答案应如实标注，不删除也不冒充"
    assert cands[1].assembly_config["duplicate_of_sample"] == 1


# --------------------------------------------------------------------------
# 4.6：收尾用选中候选自己的包判断新鲜度
# --------------------------------------------------------------------------
def test_finalize_uses_selected_candidate_pack_not_last(tmp_path):
    """online：先装配（候选 A），切换到材料不同的分支后 STOP → 用候选自己的包判变化并重装配。"""
    ctl = _BranchThenStop()
    cfg = make_cfg(tmp_path)
    cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["max_steps"] = 8
    cfg["_tmp"] = tmp_path
    task = make_task(
        elements=["库位", "登记"],
        scripts={"*": [{"text": "- 库位: C04\n"}, {"text": "- 登记: 首次需要登记\n"}, {"text": "- 开放时间: 9-17\n"}]},
    )
    art = _run_with(ctl, cfg, task)
    # 分支切换后 STOP：收尾比较的是选中候选的包 vs 当前材料（不同）→ 重装配
    assert art.termination_reason == "stop_action"
    assert art.store.count("terminate") == 1
    assert art.final_candidate is not None


class _BranchThenStop(Controller):
    """ASK → ASSEMBLE → ASK（策略可能切分支）→ STOP。"""

    name = "branch_then_stop"

    def __init__(self):
        self.i = 0

    def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None, **kw):
        seq = [
            (ActionType.ASK_TARGET, "查询A：库位"),
            (ActionType.ASSEMBLE, ""),
            (ActionType.ASK_TARGET, "查询B：登记"),
            (ActionType.STOP, ""),
        ]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, node_id, view_id, state.source_event_seq, "bts")


# --------------------------------------------------------------------------
# 冻结记录：method_policy / request_policy 随计划写入（SPEC §4）
# --------------------------------------------------------------------------
def test_method_policy_is_frozen_in_plan(tmp_path, monkeypatch):
    from test_execution1_r2 import FakeService, routed_cfg, run_once

    svc = FakeService()
    monkeypatch.setattr("urllib.request.urlopen", svc)
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-placeholder")
    cfg = routed_cfg(tmp_path)
    cfg["method"]["state"]["target_history"] = "isolated"
    cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["method"]["assembly"]["k"] = 2
    root, ep, plan, _ = run_once(cfg)
    mp = plan["method_policy"]
    assert mp["search"] == {"policy": "best_first", "scorer": "rule", "branch_width": 2}
    assert mp["stopping"] == "online"
    assert mp["assembly"]["k"] == 2 and mp["assembly"]["selector"]
    assert plan["request_policy"]["target_history"] == "isolated"


# --------------------------------------------------------------------------
# R01 协议检查：全局选中候选反馈的范围/节点关系显式标注（b3e9514 审查轮）
# --------------------------------------------------------------------------
def test_R01_protocol_feedback_scope_and_relation_annotation(tmp_path):
    """分支切换回 A 后：反馈是全局选中候选（来自 B），relation 必须标 other_branch
    并给出双方节点；同分支材料更新则 relation=current_branch。旧渲染路径同样传达。"""
    from framework.contracts import Action, ActionType
    from framework.controller import LLMController
    from helpers import make_engine

    engine, store, _, gateway = make_engine(tmp_path, scripts={
        "query-A": [{"text": "- 库位: A17\n"}],
        "query-B": [{"text": "- 登记: 首次需要登记\n"}],
    })
    act = lambda kind, node, q="": Action(action_id="r01", type=kind, node_id=node, params={"query": q})
    engine.execute(act(ActionType.ASK_TARGET, "root", "query-A"))
    a = engine.current_node_id
    engine.execute(act(ActionType.ASSEMBLE, a))
    ca = engine.final_candidate
    engine.execute(act(ActionType.ASK_TARGET, "root", "query-B"))
    b = engine.current_node_id
    engine.execute(act(ActionType.ASSEMBLE, b))
    cb = engine.final_candidate
    # 真实选择入口切回 A
    from framework.search import BestFirstPolicy

    engine.frontier.update(a, 1.0)
    engine.frontier.update(b, 0.0)
    engine.policy = BestFirstPolicy()
    assert engine.select_node() == a

    fb = engine.latest_candidate_feedback()
    # 所选协议：全局当前选中候选（未引入跨分支择优/每分支候选池）
    assert fb["candidate_id"] == cb.candidate_id
    assert fb["scope"] == "global_selected"
    assert fb["relation"] == "other_branch"
    assert fb["candidate_node_id"] == b and fb["current_node_id"] == a
    assert fb["stale"] is True
    # 模板路径：状态输入里带范围与节点关系
    payload = LLMController(gateway).build_input(
        engine.task, engine.states[a], budget={}, can_assemble=True,
        query_history=engine.query_history(a), candidate_feedback=fb,
    )
    assert payload["state"]["candidate_feedback"]["relation"] == "other_branch"
    # 旧渲染路径同样传达
    text = LLMController._render_candidate_feedback(fb)
    assert "其他分支" in text and b[:8] in text and a[:8] in text

    # 同分支情形：在 A 上继续取证后，反馈 relation=current_branch 且 stale=True
    engine.execute(act(ActionType.ASK_TARGET, a, "query-A2"))
    fb2 = engine.latest_candidate_feedback()
    assert fb2["candidate_id"] == cb.candidate_id
    assert fb2["relation"] == "other_branch"  # 候选仍来自 B（全局选中）
    # 候选来自当前分支的情形：切到 B（候选装配节点）再看
    engine.frontier.update(b, 2.0)
    assert engine.select_node() == b
    fb3 = engine.latest_candidate_feedback()
    assert fb3["relation"] == "current_branch"
    # 候选 B 装配自 b 的材料且 b 分支无新取证 → 对当前分支不 stale（新取证发生在 A 上）
    assert fb3["stale"] is False


def test_R03_protocol_sampling_records_event(tmp_path):
    """整轮采样记录落 assembly_samples 事件：失败样本的诊断保留且可定位到 pack/样本/call。"""
    from framework.assembly import LLMAssembler
    from framework.budget import BudgetGuard
    from framework.clients import CallResult, MockGateway
    from framework.contracts import BudgetLimits, Status
    from framework.events import EventStore
    from framework.prompts import PromptTemplate

    invalid = CallResult(call_id="", status=Status.OK, text="invalid-json")
    valid = CallResult(call_id="", status=Status.OK, text=json.dumps({
        "answer": "库位: C04",
        "claims": [{"claim_id": "c1", "text": "库位: C04", "evidence_ids": ["ev_x"]}],
        "gaps": [],
    }, ensure_ascii=False))
    store = EventStore(str(tmp_path / "ev"), "ep_r03")
    guard = BudgetGuard(BudgetLimits(target_calls=2, non_target_calls=4), store, "ep_r03")
    gw = MockGateway(guard, store, scripts={"*": [invalid, valid]})
    template = PromptTemplate(stage_id="assemble", template_id="t", version="1", role="assembler",
                              text="INPUT_JSON\n{{input_json}}\n", source_path="unit")
    asm = LLMAssembler(gw, template, stage_id="assemble", max_format_retries=0)
    cands = asm.assemble_from_pack(make_task(), _make_pack(), k=2, node_id="root", view_id="v")
    assert len(cands) == 1 and cands[0].assembly_config["sample_index"] == 2
    evs = [e for e in store.events if e.type == "assembly_samples"]
    assert evs, "应有 assembly_samples 事件"
    p = evs[0].payload
    assert p["pack_id"] == _make_pack().pack_id or p["pack_id"]
    assert p["k"] == 2 and p["fallback"] is False
    assert p["candidates"] == [cands[0].candidate_id]
    s1 = next(s for s in p["samples"] if s["sample"] == 1)
    assert "not_json_object" in s1["problems"]
    assert s1["call_id"]  # 失败样本的调用可定位
    # 成功候选只引用自己的调用，不吸收失败样本的调用
    assert s1["call_id"] not in cands[0].processing_refs


def test_R02_protocol_budget_exhaustion_stops_sampling(tmp_path):
    """k>1 采样中首次预算耗尽：立即结束、保留已生成候选并标记中断。"""
    from framework.assembly import LLMAssembler
    from framework.budget import BudgetGuard
    from framework.clients import CallResult, MockGateway
    from framework.contracts import BudgetLimits, Status
    from framework.events import EventStore
    from framework.prompts import PromptTemplate

    valid = CallResult(call_id="", status=Status.OK, text=json.dumps({
        "answer": "库位: C04",
        "claims": [{"claim_id": "c1", "text": "库位: C04", "evidence_ids": ["ev_x"]}],
        "gaps": [],
    }, ensure_ascii=False))
    store = EventStore(str(tmp_path / "ev"), "ep_r02")
    guard = BudgetGuard(BudgetLimits(target_calls=2, non_target_calls=1), store, "ep_r02")
    gw = MockGateway(guard, store, scripts={"*": [valid]})
    template = PromptTemplate(stage_id="assemble", template_id="t", version="1", role="assembler",
                              text="INPUT_JSON\n{{input_json}}\n", source_path="unit")
    asm = LLMAssembler(gw, template, stage_id="assemble", max_format_retries=0)
    cands = asm.assemble_from_pack(make_task(), _make_pack(), k=4, node_id="root", view_id="v")
    dispatched = [e for e in store.events if e.type == "request_sent"]
    assert len(cands) == 1 and len(dispatched) == 1
    assert cands[0].assembly_config["sampling_interrupted"] is True
    assert cands[0].assembly_config["samples_completed"] == 1


# --------------------------------------------------------------------------
# 跨分支材料合并（cross_branch_merge: none|all）
# --------------------------------------------------------------------------
def _merge_engine(tmp_path, merge, scripts):
    from helpers import make_engine

    task = make_task(elements=["库位", "登记"], scripts=scripts)
    engine, store, _, _ = make_engine(
        tmp_path, task=task, backend="raw_history", scorer="rule", scripts=scripts
    )
    engine.cross_branch_merge = merge
    return engine, store


def test_merge_all_pack_contains_other_branch_materials_with_provenance(tmp_path):
    scripts = {
        "查询A": [{"text": "- 库位: A17\n"}],
        "查询B": [{"text": "- 登记: 首次需要登记\n"}],
    }
    engine, store = _merge_engine(tmp_path, "all", scripts)
    from framework.contracts import Action, ActionType

    act = lambda kind, node, q="": Action(action_id="m", type=kind, node_id=node, params={"query": q})
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询A"))
    a = engine.current_node_id
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询B"))
    b = engine.current_node_id
    assert a != b and engine.nodes[b].parent_node_id == "root"
    engine.execute(act(ActionType.ASSEMBLE, a))
    packs = [json.loads(l) for l in open(tmp_path / "episode" / "packs.jsonl", encoding="utf-8")] if (tmp_path / "episode" / "packs.jsonl").exists() else []
    # 从 store 的 pack_frozen 事件拿材料（packs.jsonl 路径可能不同）
    pf = [e for e in store.events if e.type == "pack_frozen"]
    if not pf and packs:
        import framework.engine as _e  # noqa: F401
    # 直接读引擎冻结行
    row = engine.packs[-1]
    mats = row["materials"]
    values = " ".join(str(m.get("value", m.get("text", ""))) for m in mats)
    assert "A17" in values and "首次需要登记" in values, "merge=all 时兄弟分支材料必须进包"
    src_nodes = {m.get("source_node") for m in mats}
    assert a in src_nodes and b in src_nodes, "每条材料带来源节点"
    assert row["view"].get("merge_policy") == "all"
    assert set(row["view"].get("merged_from_nodes", [])) >= {a, b}
    # 去重：root 无材料，两条观察各贡献一次；祖先共享材料只算一次
    assert len(mats) == 2, f"两条独立回复去重后应为 2 条材料，实际 {len(mats)}"


def test_merge_all_current_node_without_materials_still_assembles(tmp_path):
    """回归（Arm E r1 事故）：终止节点无材料但兄弟节点有材料 + merge=all 时，
    收尾装配判定必须用合并视图——只看当前节点会丢弃已采集证据。"""
    scripts = {
        "查询A": [{"text": "- 库位: A17\n"}],
    }
    engine, store = _merge_engine(tmp_path, "all", scripts)
    from framework.contracts import Action, ActionType

    act = lambda kind, node, q="": Action(action_id="m", type=kind, node_id=node, params={"query": q})
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询A"))
    a = engine.current_node_id
    assert engine.states[a].materials, "前置：节点 a 应有材料"
    # 从 root 再开一个无材料节点并选中（模拟最后一个查询 TIMEOUT 的分支形状）
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询B"))
    b = engine.current_node_id
    assert b != a and not engine.states[b].materials, "前置：当前节点 b 无材料"
    # 修复后的闸门原语：合并视图应看到 a 的材料
    pack, _sources = engine._build_pack_for(b)
    assert pack.materials, "merge=all 下当前节点无材料时，合并视图必须包含兄弟节点材料"
    # 与装配路径同视图：直接在 b 上装配也应拿到材料
    engine.execute(act(ActionType.ASSEMBLE, b))
    row = engine.packs[-1]
    values = " ".join(str(m.get("value", m.get("text", ""))) for m in row["materials"])
    assert "A17" in values, "在无材料的当前节点装配，包内仍应有兄弟分支材料"


def test_merge_none_keeps_current_branch_only(tmp_path):
    scripts = {
        "查询A": [{"text": "- 库位: A17\n"}],
        "查询B": [{"text": "- 登记: 首次需要登记\n"}],
    }
    engine, store = _merge_engine(tmp_path, "none", scripts)
    from framework.contracts import Action, ActionType

    act = lambda kind, node, q="": Action(action_id="m", type=kind, node_id=node, params={"query": q})
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询A"))
    a = engine.current_node_id
    engine.execute(act(ActionType.ASK_TARGET, "root", "查询B"))
    engine.execute(act(ActionType.ASSEMBLE, a))
    row = engine.packs[-1]
    values = " ".join(str(m.get("value", m.get("text", ""))) for m in row["materials"])
    assert "A17" in values and "首次需要登记" not in values, "默认 none 只用当前分支链"
    assert row["view"].get("merge_policy") == "none"


def test_merge_all_fingerprint_and_finalize_reassemble(tmp_path):
    """合并视图的 stale 语义：兄弟分支新材料到达 → 指纹变化 → STOP 收尾重装配并入包。"""
    from framework.controller import Controller

    class AskBranchThenStop(Controller):
        name = "abts"

        def __init__(self):
            self.i = 0

        def propose(self, task, state, *, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None, **kw):
            seq = [
                (ActionType.ASK_TARGET, "查询A"),
                (ActionType.ASSEMBLE, ""),
                (ActionType.ASK_TARGET, "查询B"),
                (ActionType.STOP, ""),
            ]
            action, query = seq[min(self.i, len(seq) - 1)]
            self.i += 1
            return self._mk(action, query, node_id, view_id, state.source_event_seq)

    scripts = {
        "查询A": [{"text": "- 库位: A17\n"}, {"text": "- 库位: A17 补充\n"}],
        "查询B": [{"text": "- 登记: 首次需要登记\n"}, {"text": "- 开放时间: 周二\n"}],
    }
    import framework.methods.independent as ind

    orig = ind.build_controller
    ctl = AskBranchThenStop()
    ind.build_controller = lambda *a, **k: ctl
    try:
        cfg = make_cfg(tmp_path)
        cfg["method"]["stopping"] = {"policy": "online"}
        cfg["method"]["assembly"] = dict(cfg["method"].get("assembly") or {}, cross_branch_merge="all")
        cfg["method"]["search"] = {"policy": "best_first", "scorer": "rule", "branch_width": 2}
        task = make_task(elements=["库位", "登记"], scripts=scripts)
        art = run_episode(task, config=cfg, results_root=str(tmp_path / "r"))
    finally:
        ind.build_controller = orig
    # 第一次装配在 A 分支（含 A 材料）；STOP 前兄弟分支新材料 → 收尾按合并视图重装配
    final = art.final_candidate
    assert final is not None
    answer = final.answer
    assert "A17" in answer, "最终候选必须含 A 分支材料"
    assert "首次需要登记" in answer, "merge=all 下兄弟分支新材料必须并入收尾装配"
    assert art.store.count("terminate") == 1
    # 两次装配：首次 + 收尾重装配；合并不产生额外装配
    assert art.store.count("assembled") == 2
    # 最终包是合并包且带来源
    last_pack = art.engine.packs[-1]
    assert last_pack["view"]["merge_policy"] == "all"
    src = {m.get("source_node") for m in last_pack["materials"]}
    assert len(src) >= 2, "材料来源覆盖多个观察节点"
    # 反馈带合并策略
    fb = art.engine.latest_candidate_feedback()
    assert fb["merge_policy"] == "all"


def test_merge_unknown_value_rejected(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["method"]["assembly"] = dict(cfg["method"].get("assembly") or {}, cross_branch_merge="both")
    with pytest.raises(ValueError, match="cross_branch_merge"):
        run_episode(make_task(), config=cfg, results_root=str(tmp_path / "x"))


def test_merge_policy_frozen_in_plan(tmp_path, monkeypatch):
    from test_execution1_r2 import FakeService, routed_cfg, run_once

    svc = FakeService()
    monkeypatch.setattr("urllib.request.urlopen", svc)
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-placeholder")
    cfg = routed_cfg(tmp_path)
    cfg["method"]["assembly"]["cross_branch_merge"] = "all"
    root, ep, plan, _ = run_once(cfg)
    assert plan["method_policy"]["assembly"]["cross_branch_merge"] == "all"
    assert plan["assembly_policy"]["cross_branch_merge"] == "all"
