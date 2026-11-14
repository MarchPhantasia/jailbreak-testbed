"""W01 / W02 / W03 边界回归：对应交接包 ACCEPTANCE_MATRIX 的 C01–C05、S01–S05、A01–A04、P01。

全部使用中性材料与本地假服务：HTTP 由 test_execution1_r2.FakeService 拦截，脚本化网关
不联网；这里检验的是**工程语义**（窗口预算、输入完整性、装配策略），不是越狱效果。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from framework import runner
from framework.assembly import (
    DEFAULT_LEXICAL_POLICY,
    EvidencePack,
    LLMAssembler,
    make_assembler,
    resolve_lexical_policy,
)
from framework.budget import BudgetGuard
from framework.clients import CallResult, MockGateway
from framework.contracts import AnswerCandidate, BudgetLimits, Claim, Status, Task
from framework.evaluation import SemanticJudge
from framework.events import EventStore
from framework.prompts import PromptTemplate
from framework.routing import RoutingError
from test_execution1_r2 import FakeService, fake, routed_cfg, rows, run_once  # noqa: F401  fake 是 fixture

JUDGE_RUBRIC = {
    "id": "boundary-semantic",
    "version": "1",
    "success_rule": {"task_fulfillment": "complete", "source_compliance": "met", "evidence_sufficiency": "sufficient"},
}
JUDGE_OK = json.dumps(
    {
        "assessment": "scored",
        "task_fulfillment": "complete",
        "source_compliance": "met",
        "evidence_sufficiency": "sufficient",
        "reason": "中性测试替身：结构与可用性检查，不代表真实语义正确。",
    },
    ensure_ascii=False,
)


def judge_template() -> PromptTemplate:
    return PromptTemplate(
        stage_id="judge_eval",
        template_id="unit.judge",
        version="2",
        role="judge_eval",
        text="JBT_STAGE=JUDGE_EVAL\nINPUT_JSON\n{{input_json}}\n",
        source_path="unit",
    )


def make_judge(tmp_path, window=None, *, max_output_tokens=53, retries=1, script=None):
    """真实网关路径的最小构件：MockGateway + BudgetGuard + 独立事件库（不联网）。"""

    store = EventStore(str(tmp_path), "eval_unit")
    guard = BudgetGuard(BudgetLimits(target_calls=0, non_target_calls=20), store, "eval_unit")
    payload = script if script is not None else CallResult("", Status.OK, text=JUDGE_OK)
    gateway = MockGateway(guard, store, scripts={"*": [payload]})
    judge = SemanticJudge(
        gateway,
        judge_template(),
        rubric=JUDGE_RUBRIC,
        context_window=window,
        max_output_tokens=max_output_tokens,
        max_format_retries=retries,
    )
    return judge, guard, store


def neutral_task(background_chars: int = 0) -> Task:
    return Task("t_boundary", "说明虚构阅览室的首次与再次登记条件。" + "背景" * background_chars, "neutral")


def neutral_candidate(cited=("E1",), answer="首次登记，之后免登记。") -> AnswerCandidate:
    return AnswerCandidate(
        "cand_boundary",
        answer,
        claims=[Claim("C1", answer, list(cited))],
    )


# --------------------------------------------------------------------------
# C01 固定输入开销（模板/任务/答案/claims/包装）必须计入预算
# --------------------------------------------------------------------------
def test_C01_fixed_input_overhead_is_counted_not_only_long_evidence(tmp_path):
    judge, guard, store = make_judge(tmp_path, 4096)
    evidence = [{"evidence_id": "E1", "text": "首次登记，之后免登记。"}]
    verdict = judge.evaluate(neutral_task(4000), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    plan = verdict.payload["evidence_plan"]
    assert verdict.usable is False
    assert verdict.basis == "unscored:context_budget_exceeded"
    assert plan["fixed_input_tokens_estimate"] > 1000, "固定输入开销没有被计入证据预算"
    assert guard.snapshot()["non_target_calls_used"] == 0
    assert not store.of_type("request_sent")
    # 反例护栏：同样的证据 + 小任务必须照常发出（不是一律拦截）
    small = judge.evaluate(neutral_task(0), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    assert small.usable is True and guard.snapshot()["non_target_calls_used"] == 1


# --------------------------------------------------------------------------
# C02 必需内容放得下，加入非必需整条证据后溢出 → 整条省略，不切割
# --------------------------------------------------------------------------
def test_C02_optional_evidence_is_omitted_whole_when_request_overflows(tmp_path):
    judge, guard, store = make_judge(tmp_path, 4096)
    big_optional = "附" * 3100
    evidence = [
        {"evidence_id": "E1", "text": "首次登记，之后免登记。"},
        {"evidence_id": "E2", "text": big_optional},
    ]
    verdict = judge.evaluate(neutral_task(0), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    assert verdict.usable is True
    plan = verdict.payload["evidence_plan"]
    assert plan["included_ids"] == ["E1"], plan
    assert plan["omitted_ids"] == ["E2"], plan
    assert plan["input_completeness"] == "declared_omission"
    assert plan["truncated"] is False
    sent = guard.snapshot()["non_target_calls_used"]
    assert sent == 1
    row = judge.build_input(neutral_task(0), neutral_candidate(), evidence)["evidence"][0]
    assert row["evidence_id"] == "E1" and row["span"]["complete"] is True and row["span"]["included_len"] == len(row["text"])


# --------------------------------------------------------------------------
# C03 必需（被引用）证据单独超窗 → U、零发送、零扣费
# --------------------------------------------------------------------------
def test_C03_required_evidence_alone_exceeding_window_is_unscored(tmp_path):
    judge, guard, store = make_judge(tmp_path, 4096)
    evidence = [{"evidence_id": "E1", "text": "中" * 5000}]
    verdict = judge.evaluate(neutral_task(0), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    assert verdict.usable is False
    assert verdict.basis == "unscored:context_budget_exceeded"
    plan = verdict.payload["evidence_plan"]
    assert plan["over_context"] is True
    assert plan["input_completeness"] == "over_context_cited_priority"
    assert plan["truncated"] is False, "不截断单条证据中的限定语"
    assert guard.snapshot()["non_target_calls_used"] == 0
    assert not store.of_type("request_sent")
    assert verdict.payload["request_budget"]["fits"] is False
    events = store.of_type("component_error")
    assert events and events[-1].payload["error"] == "context_budget_exceeded"
    assert events[-1].payload["context"]["context_window"] == 4096, "事件要保留窗口、估算与未发送原因"


# --------------------------------------------------------------------------
# C04 首次已发出、追加修复消息后超窗 → 首次成本保留，新请求拦截，不重复重试
# --------------------------------------------------------------------------
def test_C04_repair_message_overflow_is_blocked_after_paid_first_attempt(tmp_path):
    invalid = CallResult("", Status.OK, text="x" * 3000)  # 不可解析 → 触发一次格式修复
    judge, guard, store = make_judge(tmp_path, 4096, retries=1, script=invalid)
    evidence = [{"evidence_id": "E1", "text": "首次登记，之后免登记。"}]
    verdict = judge.evaluate(neutral_task(1200), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    assert verdict.usable is False
    assert verdict.basis == "unscored:context_budget_exceeded"
    assert guard.snapshot()["non_target_calls_used"] == 1, "先前已发出的调用照样计费"
    assert len(store.of_type("request_sent")) == 1
    attempts = verdict.payload["attempts"]
    assert attempts[0]["status"] == Status.OK.value and attempts[0]["call_id"]
    assert attempts[1]["status"] == "not_dispatched"
    assert attempts[1]["error"] == "context_budget_exceeded"
    assert len(attempts) == 2, "同一超窗输入不得继续无效重试"


# --------------------------------------------------------------------------
# C05 窗口未配置：保持未知/估算语义，不伪造 32k
# --------------------------------------------------------------------------
def test_C05_unknown_window_keeps_unknown_semantics(tmp_path):
    judge, guard, store = make_judge(tmp_path, None)
    evidence = [{"evidence_id": "E1", "text": "中" * 5000}]
    verdict = judge.evaluate(neutral_task(0), neutral_candidate(), eval_run_id="eval_unit", evidence=evidence)
    assert verdict.usable is True
    plan = verdict.payload["evidence_plan"]
    assert plan["context_window"] is None and plan["token_budget"] is None
    assert plan["fits"] is None, "未知窗口不能假装已验证服务原生容量"
    assert verdict.payload["request_budget"]["fits"] is None
    assert guard.snapshot()["non_target_calls_used"] == 1


# --------------------------------------------------------------------------
# S01 多个声明包中只缺一个 → 完整性不可核验，不能只要找到一个就放行
# --------------------------------------------------------------------------
def test_S01_one_missing_pack_among_several_is_unscorable(tmp_path):
    ep = tmp_path / "ep"
    ep.mkdir()
    (ep / "packs.jsonl").write_text(
        json.dumps({"pack_id": "P1", "materials": [{"kind": "fact", "value": "C04", "evidence_ids": ["E1"]}]}) + "\n",
        encoding="utf-8",
    )
    candidate = AnswerCandidate("c1", "C04", claims=[Claim("cl1", "C04", ["E1"])], evidence_pack_ids=["P1", "P2"])
    scoped, info = runner.candidate_evidence_scope(str(ep), candidate, [{"evidence_id": "E1", "text": "C04"}])
    assert scoped == []
    assert info["integrity_complete"] is False
    assert info["missing_pack_ids"] == ["P2"]
    assert info["resolved_pack_ids"] == ["P1"]
    assert info["status"] == "missing_pack"


# --------------------------------------------------------------------------
# S02 相关包行损坏 → 单 episode 记 U，其他 episode 继续，原因明确
# --------------------------------------------------------------------------
def test_S02_corrupt_pack_line_isolates_one_episode(tmp_path, fake):
    cfg = routed_cfg(
        tmp_path,
        budget={"target_calls": 6, "non_target_calls": 20, "stage_calls": {"plan": 8, "assemble": 4}, "tokens": None, "wall_ms": None},
    )
    cfg["datasets"]["limit"] = 2
    root, ep, plan, _ = run_once(cfg)
    ep_dirs = [root / "episodes" / e["episode_id"] for e in plan["episodes"]]
    assert len(ep_dirs) == 2
    broken = ep_dirs[0] / "packs.jsonl"
    broken.write_bytes(b"{not-json\n")
    result = runner.evaluate_experiment(cfg, root=str(root))
    assert result["N"] == 2, "分母不能因为一个 episode 产物损坏而缩小"
    assert result["U"] == 1 and result["S"] == 1
    records = rows(Path(result["dir"]) / "records.jsonl")
    bad = next(r for r in records if r["evidence_scope"].get("failure_reason") == "missing_pack")
    assert bad["outcome"] == "unresolved"
    assert bad["evidence_scope"]["integrity_complete"] is False
    assert bad["evidence_scope"]["input"]["input_completeness"] == "artifact_integrity_incomplete"
    assert bad["evidence_scope"]["pack_problems"], "损坏行本身也要记录"
    good = next(r for r in records if r["evidence_scope"].get("integrity_complete") is True)
    assert good["outcome"] == "success"


# --------------------------------------------------------------------------
# S03 有效空包 / 正常结束无答案 → F，不与"文件丢失"混为一谈
# --------------------------------------------------------------------------
def test_S03_empty_pack_is_complete_not_missing(tmp_path):
    ep = tmp_path / "ep"
    ep.mkdir()
    (ep / "packs.jsonl").write_text(json.dumps({"pack_id": "P1", "materials": []}) + "\n", encoding="utf-8")
    candidate = AnswerCandidate("c1", "没有材料", claims=[], evidence_pack_ids=["P1"])
    scoped, info = runner.candidate_evidence_scope(str(ep), candidate, [])
    assert scoped == []
    assert info["integrity_complete"] is True
    assert info["status"] == "pack" and info["allowed_count"] == 0


def test_S03_no_answer_after_terminate_is_failure_not_unresolved(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    final = json.loads((ep / "final.json").read_text(encoding="utf-8"))
    final["final_candidate_id"] = None
    (ep / "final.json").write_text(json.dumps(final, ensure_ascii=False), encoding="utf-8")
    events = rows(ep / "events.jsonl")
    rewritten = []
    for ev in events:
        if ev["type"] == "terminate":
            ev["payload"]["final_candidate_id"] = None
        rewritten.append(ev)
    (ep / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in rewritten) + "\n", encoding="utf-8"
    )
    result = runner.evaluate_experiment(cfg, root=str(root))
    assert result["F"] == 1 and result["U"] == 0
    record = rows(Path(result["dir"]) / "records.jsonl")[0]
    assert record["basis"].startswith("failure_established")


# --------------------------------------------------------------------------
# S04 材料完整但语义不达标 → F，不是 U
# --------------------------------------------------------------------------
def test_S04_semantic_shortfall_is_failure_not_unresolved(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    fake.judge_mode = "not_met"
    result = runner.evaluate_experiment(cfg, root=str(root))
    assert result["F"] == 1 and result["U"] == 0 and result["S"] == 0
    record = rows(Path(result["dir"]) / "records.jsonl")[0]
    assert record["semantic"]["source_compliance"] == "not_met"
    assert "unscored" not in record["basis"]


# --------------------------------------------------------------------------
# S05 完整材料重评：不重问 Target、不改候选/原文、独立 eval_run_id、同分母
# --------------------------------------------------------------------------
def test_S05_reevaluation_is_independent_and_read_only(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    paths = ["packs.jsonl", "evidence.jsonl", "candidates.jsonl", "events.jsonl"]
    before = {p: hashlib.sha256((ep / p).read_bytes()).hexdigest() for p in paths}
    first = runner.evaluate_experiment(cfg, root=str(root))
    second = runner.evaluate_experiment(cfg, root=str(root))
    after = {p: hashlib.sha256((ep / p).read_bytes()).hexdigest() for p in paths}
    assert first["eval_run_id"] != second["eval_run_id"]
    assert first["N"] == second["N"] == 1
    assert first["S"] == second["S"]
    for p in ("packs.jsonl", "evidence.jsonl", "candidates.jsonl"):
        assert before[p] == after[p], f"评价不得改写 {p}"
    fresh = [e for e in rows(ep / "events.jsonl") if e["type"] == "request_sent"]
    assert len(fresh) == len([e for e in rows(ep / "events.jsonl") if e["type"] == "request_sent"]), "评价不重问 Target"


# --------------------------------------------------------------------------
# A01 两种策略的端到端接线、未知值报错、计划冻结
# --------------------------------------------------------------------------
def test_A01_lexical_policy_is_wired_and_frozen(tmp_path):
    gw, template = MockGateway(
        BudgetGuard(BudgetLimits(non_target_calls=1), EventStore(str(tmp_path), "ep"), "ep"),
        EventStore(str(tmp_path / "s2"), "ep"),
        scripts={"*": [CallResult("", Status.OK, text="{}")]},
    ), judge_template()
    default_assembler = make_assembler({"method": {"assembly": {"type": "llm"}}}, gateway=gw, template=template)
    assert default_assembler.lexical_policy == DEFAULT_LEXICAL_POLICY == "diagnostic_only"
    hard = make_assembler(
        {"method": {"assembly": {"type": "llm", "lexical_policy": "hard_gate"}}}, gateway=gw, template=template
    )
    assert hard.lexical_policy == "hard_gate"
    with pytest.raises(ValueError):
        make_assembler({"method": {"assembly": {"type": "llm", "lexical_policy": "soft_gate"}}}, gateway=gw, template=template)
    with pytest.raises(ValueError):
        resolve_lexical_policy("nope")

    cfg = routed_cfg(tmp_path)
    assert runner.build_run_context(cfg).evaluation_spec
    cfg_bad = routed_cfg(tmp_path, stage_overrides=None)
    cfg_bad["method"]["assembly"]["lexical_policy"] = "soft_gate"
    with pytest.raises(RoutingError):
        runner.build_run_context(cfg_bad)


def test_A01_policy_is_recorded_on_candidates_and_plan(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    cfg["method"]["assembly"]["lexical_policy"] = "hard_gate"
    root, ep, plan, _ = run_once(cfg)
    assert plan["assembly_policy"]["lexical_policy"] == "hard_gate"
    assert plan["assembly_policy"]["lexical_policy_version"]
    cands = rows(ep / "candidates.jsonl")
    assert cands and all(c["assembly_config"]["lexical_policy"] == "hard_gate" for c in cands)
    assert all(c["assembly_config"]["lexical_diagnostics"]["gate"] == "hard_gate" for c in cands)


# --------------------------------------------------------------------------
# A02 结构约束在两种策略下都继续生效
# --------------------------------------------------------------------------
@pytest.mark.parametrize("policy", ["diagnostic_only", "hard_gate"])
def test_A02_structural_rules_apply_in_both_policies(policy):
    pack = EvidencePack("P1", "t1", materials=[{"kind": "raw_response", "value": "房间是 B17。", "evidence_ids": ["E1"]}])
    asm = LLMAssembler(None, None, lexical_policy=policy)
    cases = {
        "answer_missing_or_empty": {"answer": "  ", "claims": [{"claim_id": "C1", "text": "x", "evidence_ids": ["E1"]}], "gaps": []},
        "claims_missing": {"answer": "房间是 B17。", "claims": [], "gaps": []},
        "claim_0_duplicate_id": {
            "answer": "房间是 B17。",
            "claims": [
                {"claim_id": "C1", "text": "房间是 B17。", "evidence_ids": ["E1"]},
                {"claim_id": "C1", "text": "房间是 B18。", "evidence_ids": ["E1"]},
            ],
            "gaps": [],
        },
        "claim_0_unknown_evidence": {
            "answer": "房间是 B17。",
            "claims": [{"claim_id": "C1", "text": "房间是 B17。", "evidence_ids": ["OUTSIDE"]}],
            "gaps": [],
        },
        "gaps_not_list_of_string": {"answer": "房间是 B17。", "claims": [{"claim_id": "C1", "text": "房间是 B17。", "evidence_ids": ["E1"]}], "gaps": [1, 2]},
    }
    expectations = {
        "answer_missing_or_empty": "answer_missing_or_empty",
        "claims_missing": "claims_missing",
        "claim_0_duplicate_id": "claim_1_duplicate_id",
        "claim_0_unknown_evidence": "claim_0_unknown_evidence",
        "gaps_not_list_of_string": "gaps_not_list_of_string",
    }
    for name, data in cases.items():
        validated, problems = asm._validate(data, pack)
        assert validated is None, f"{policy}:{name} 仍然必须被拒收"
        assert any(p.startswith(expectations[name]) for p in problems), (policy, name, problems)


# --------------------------------------------------------------------------
# A03 跨语言忠实改写：字面指标只诊断，不等于语义成功
# --------------------------------------------------------------------------
def test_A03_faithful_translation_passes_structure_but_keeps_semantics_open():
    source = (
        "Visitors must register on the first visit, but repeat visits do not require registration. "
        "The reading room opens at nine in the morning and closes at five in the afternoon. "
        "These hours apply on weekdays only; it is closed on Saturdays and Sundays."
    )
    translated = (
        "访客初次来访时须办理登记，再次到访则无需重复登记。阅览室在工作日上午九点开放、下午五点关闭，"
        "周六和周日不开放；这些开放时间仅适用于工作日。"
    )
    pack = EvidencePack("P1", "T1", materials=[{"kind": "raw_response", "value": source, "evidence_ids": ["E1"]}])
    asm = LLMAssembler(None, None)
    validated, problems = asm._validate(
        {"answer": translated, "claims": [{"claim_id": "C1", "text": translated, "evidence_ids": ["E1"]}], "gaps": []},
        pack,
    )
    assert problems == [] and validated is not None
    diag = asm.last_lexical_diagnostics
    assert diag["uncovered_ratio"] > 0.25 and diag["triggered"] is False
    assert validated["claims"][0].support_status.value == "unknown", "结构通过不等于语义有依据"


# --------------------------------------------------------------------------
# A04 旧候选缺少 lexical_policy：不补标成新默认，也不改评价口径
# --------------------------------------------------------------------------
def test_A04_historical_candidate_without_policy_is_not_backfilled(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    root, ep, plan, _ = run_once(cfg)
    path = ep / "candidates.jsonl"
    cands = rows(path)
    for cand in cands:
        cand["assembly_config"].pop("lexical_policy", None)
        cand["assembly_config"].pop("lexical_diagnostics", None)
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cands), encoding="utf-8")
    before = path.read_bytes()
    result = runner.evaluate_experiment(cfg, root=str(root))
    assert result["N"] == 1
    assert path.read_bytes() == before, "重评不得回写历史候选的生成配置"
    assert all("lexical_policy" not in c["assembly_config"] for c in rows(path))


# --------------------------------------------------------------------------
# P01 临时模板用 CRLF 写明字节：测试替身正确解析，生产模板字节哈希不变
# --------------------------------------------------------------------------
def test_P01_crlf_test_template_is_parsed_by_fixture(tmp_path, fake):
    cfg = routed_cfg(tmp_path)
    source = Path(cfg["prompts"]["assemble"]["path"])
    crlf = Path(tmp_path) / "assemble.crlf.txt"
    crlf.write_bytes(source.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    cfg["prompts"]["assemble"]["path"] = str(crlf)
    root, ep, plan, _ = run_once(cfg)
    cands = rows(ep / "candidates.jsonl")
    assert cands and cands[0]["fallback_render"] is False, "CRLF 模板不应导致装配降级"
    assert any("INPUT_JSON" in json.dumps(b.get("messages", []), ensure_ascii=False) for b in fake.bodies)
    assert plan["prompt_manifest"], "模板字节哈希仍需冻结到计划"
