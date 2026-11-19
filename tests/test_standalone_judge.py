"""独立评价入口（Judge 与生成流程解耦）与 rubric v2 部分成功档的测试。"""
from __future__ import annotations

import json
import os

import pytest

from framework import standalone
from framework import runner
from framework.runner import _evidence_text_checksum, pack_material_hash
from framework.evaluation import (
    parse_judge_output,
    partial_success_from_rule,
    success_from_rule,
)
from framework.contracts import AnswerCandidate, Claim, Task

RUBRIC_V2 = {
    "id": "original-task-grounded-completion",
    "version": "2",
    "success_rule": {
        "task_fulfillment": "complete",
        "source_compliance": "met",
        "evidence_sufficiency": "sufficient",
    },
    "partial_success_rule": {"task_fulfillment": "partial", "sub_result": "substantive"},
    "required_output_fields": ["sub_result", "sub_result_evidence_ids", "citation_status"],
}

FULL = {
    "assessment": "scored",
    "task_fulfillment": "complete",
    "source_compliance": "met",
    "evidence_sufficiency": "sufficient",
    "sub_result": "not_applicable",
    "sub_result_statement": "无",
    "sub_result_evidence_ids": [],
    "citation_status": "clean",
    "reason": "x",
}
PARTIAL_SUB = {
    **FULL,
    "task_fulfillment": "partial",
    "evidence_sufficiency": "insufficient",
    "sub_result": "substantive",
    "sub_result_statement": "交付了 A 与 B，缺 C",
    "sub_result_evidence_ids": ["ev_1", "ev_2"],
}
PARTIAL_PLAIN = {**PARTIAL_SUB, "sub_result": "insufficient", "sub_result_statement": "无",
                 "sub_result_evidence_ids": []}


def _parsed(data):
    parsed = parse_judge_output(json.dumps(data))
    assert parsed.ok, parsed.problems
    return parsed.payload


def test_v4_fields_parsed():
    p = _parsed(PARTIAL_SUB)
    assert p["sub_result"] == "substantive"
    assert p["sub_result_evidence_ids"] == ["ev_1", "ev_2"]
    assert p["citation_status"] == "clean"


def test_v2_output_still_parses_with_degraded_new_fields():
    """旧模板输出（无 v4 字段）仍可解析：新字段降级，不破坏既有三维。"""
    legacy = {
        "assessment": "scored", "task_fulfillment": "complete", "source_compliance": "met",
        "evidence_sufficiency": "sufficient", "reason": "x",
    }
    p = _parsed(legacy)
    assert p["sub_result"] == "unknown"
    assert p["sub_result_evidence_ids"] == []
    assert p["citation_status"] == "unknown"


def test_invalid_v4_values_degrade_instead_of_failing():
    bad = {**PARTIAL_SUB, "sub_result": "very_usable", "citation_status": "maybe",
           "sub_result_evidence_ids": "ev_1"}
    p = _parsed(bad)
    assert p["sub_result"] == "unknown"
    assert p["citation_status"] == "unknown"
    assert p["sub_result_evidence_ids"] == []


def test_partial_success_rule_is_exclusive_with_full_success():
    assert partial_success_from_rule(_parsed(PARTIAL_SUB), RUBRIC_V2["partial_success_rule"],
                                     RUBRIC_V2["success_rule"]) is True
    # 完整成功优先，绝不重复计入部分成功
    assert partial_success_from_rule(_parsed(FULL), RUBRIC_V2["partial_success_rule"],
                                     RUBRIC_V2["success_rule"]) is False
    assert partial_success_from_rule(_parsed(PARTIAL_PLAIN), RUBRIC_V2["partial_success_rule"],
                                     RUBRIC_V2["success_rule"]) is False


def test_partial_success_without_rule_is_false():
    assert partial_success_from_rule(_parsed(PARTIAL_SUB), {}) is False


def test_full_success_rule_unchanged_by_new_fields():
    """新增字段不改变完整成功判定（可比性前提）。"""
    rule = RUBRIC_V2["success_rule"]
    assert success_from_rule(_parsed(FULL), rule) is True
    assert success_from_rule(_parsed(PARTIAL_SUB), rule) is False


def test_load_rubric_rejects_missing_success_rule(tmp_path):
    fp = tmp_path / "bad.json"
    fp.write_text(json.dumps({"id": "x"}), encoding="utf-8")
    with pytest.raises(ValueError):
        standalone.load_rubric(str(fp))


def test_load_rubric_default_when_absent():
    rubric = standalone.load_rubric(None)
    assert rubric["success_rule"]["task_fulfillment"] == "complete"
    assert "partial_success_rule" not in rubric


def _write_run_root(root, *, with_final=True, candidate_pack_ids=("pack_1",)):
    """最小冻结运行目录：plan + episode（final/candidates/packs/evidence）。"""
    root.mkdir(parents=True, exist_ok=True)
    plan = {
        "experiment_id": "synthetic",
        "evaluation_spec": {"type": "llm", "evidence_scope": "candidate_pack"},
        "episodes": [{
            "episode_id": "ep_1",
            "task": {"task_id": "t1", "prompt": "整理资料",
                     "primary_dataset": "synthetic"},
        }],
        "prompt_manifest": [],
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    ep = root / "episodes" / "ep_1"
    ep.mkdir(parents=True, exist_ok=True)
    if with_final:
        (ep / "final.json").write_text(json.dumps({"final_candidate_id": "cand_1"}), encoding="utf-8")
        cand = {
            "candidate_id": "cand_1", "answer": "答案正文", "claims": [
                {"claim_id": "c1", "text": "主张一", "evidence_ids": ["ev_1"]},
            ],
            "evidence_pack_ids": list(candidate_pack_ids), "gaps": [],
            "fallback_render": False,
        }
        (ep / "candidates.jsonl").write_text(json.dumps(cand) + "\n", encoding="utf-8")
    text = "证据一"
    materials = [{
        "kind": "target_response", "key": "ev_1", "value": text, "text": text,
        "evidence_ids": ["ev_1"], "support_status": "unknown", "fidelity": "verbatim",
        "source_node": "root",
    }]
    pack_hash = pack_material_hash(materials)
    (ep / "packs.jsonl").write_text(
        json.dumps({"pack_id": "pack_1", "task_id": "t1", "materials": materials, "gaps": [],
                    "notes": [], "components": [], "input_hash": pack_hash,
                    "input_hash_v2": "h2"}) + "\n",
        encoding="utf-8",
    )
    (ep / "evidence.jsonl").write_text(
        json.dumps({
            "evidence_id": "ev_1", "source_event_id": "evt_1", "call_id": "call_1",
            "node_id": "root", "role": "assistant", "text": text, "span_start": "0",
            "span_end": str(len(text)), "checksum": _evidence_text_checksum(text),
        }) + "\n",
        encoding="utf-8",
    )
    return root


def test_load_artifact_reads_existing_files_only(tmp_path):
    root = _write_run_root(tmp_path / "run")
    art = standalone.load_artifact(str(root), "t1")
    assert art is not None
    assert art.candidate.candidate_id == "cand_1"
    assert [e["evidence_id"] for e in art.evidence] == ["ev_1"]
    assert art.pack_ids == ["pack_1"]
    assert art.pack_input_hash == ["h2"]
    prov = art.provenance()
    assert prov["candidate_id"] == "cand_1"
    assert prov["files"]["final"].endswith("final.json")


def test_load_artifact_returns_none_without_final(tmp_path):
    root = _write_run_root(tmp_path / "run", with_final=False)
    assert standalone.load_artifact(str(root), "t1") is None


def test_load_artifact_returns_none_for_unknown_task(tmp_path):
    root = _write_run_root(tmp_path / "run")
    assert standalone.load_artifact(str(root), "nope") is None


def test_verdict_row_carries_both_success_channels(tmp_path):
    root = _write_run_root(tmp_path / "run")
    art = standalone.load_artifact(str(root), "t1")
    from framework.contracts import Verdict

    verdict = Verdict(
        verdict_id="vd_1", eval_run_id="eval_1", task_id="t1", answer_id="cand_1",
        rubric_version="2", judge_ref="judge@v4", cls="C", usable=True,
        basis="semantic:tf=partial,src=met,ev=insufficient,success=False",
        payload={**_parsed(PARTIAL_SUB), "success": False, "sub_success": True},
    )
    row = standalone.verdict_row(art, verdict, RUBRIC_V2)
    assert row["full_success"] is False
    assert row["partial_success"] is True
    assert row["sub_result_evidence_count"] == 2
    assert row["citation_status"] == "clean"
    assert row["pack_input_hash"] == ["h2"]


def test_summarize_counts_three_buckets(tmp_path):
    root = _write_run_root(tmp_path / "run")
    art = standalone.load_artifact(str(root), "t1")
    from framework.contracts import Verdict

    def v(cls, tf, ev, sub, statement="无", ids=None):
        return Verdict(verdict_id="vd", eval_run_id="e", task_id="t1", answer_id="cand_1",
                       rubric_version="2", judge_ref="j", cls=cls, usable=True, basis="",
                       payload={"assessment": "scored", "task_fulfillment": tf,
                                "source_compliance": "met", "evidence_sufficiency": ev,
                                "sub_result": sub, "sub_result_statement": statement,
                                "sub_result_evidence_ids": ids or [], "citation_status": "clean"})
    rows = [
        standalone.verdict_row(art, v("D", "complete", "sufficient", "not_applicable"), RUBRIC_V2),
        standalone.verdict_row(art, v("C", "partial", "insufficient", "substantive", "交付A缺B", ["ev_1"]), RUBRIC_V2),
        standalone.verdict_row(art, v("C", "partial", "insufficient", "insufficient"), RUBRIC_V2),
    ]
    s = standalone.summarize(rows)
    assert s["full_success"] == 1
    assert s["partial_success"] == 1
    assert s["neither"] == 1


def test_call_with_deadline_returns_failure_on_timeout():
    import time

    def slow():
        time.sleep(5)
        return "x"

    value, failure = standalone.call_with_deadline(slow, 0.2)
    assert value is None
    assert failure == "hard_deadline_exceeded"


def test_call_with_deadline_passes_through_without_limit():
    assert standalone.call_with_deadline(lambda: 42, 0) == (42, "")


def test_resume_skips_already_usable(tmp_path):
    prior = tmp_path / "prior.jsonl"
    prior.write_text(
        json.dumps({"task_id": "t1", "candidate_id": "cand_1", "usable": True}) + "\n"
        + json.dumps({"task_id": "t1", "candidate_id": "cand_2", "usable": False}) + "\n",
        encoding="utf-8",
    )
    keys = standalone._load_done_keys([str(prior)])
    assert keys == {("t1", "cand_1")}


def test_resume_missing_file_is_noop(tmp_path):
    assert standalone._load_done_keys([str(tmp_path / "nope.jsonl")]) == set()


def test_load_artifact_specific_candidate(tmp_path):
    """同包对照：指定 candidate_id 时读 candidates.jsonl 里的那份，而非 final。"""
    root = _write_run_root(tmp_path / "run")
    ep = root / "episodes" / "ep_1"
    other = {
        "candidate_id": "cand_2", "answer": "第二份候选", "claims": [
            {"claim_id": "c1", "text": "主张二", "evidence_ids": ["ev_1"]},
        ],
        "evidence_pack_ids": ["pack_1"], "gaps": [], "fallback_render": False,
    }
    with open(ep / "candidates.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(other) + "\n")
    art = standalone.load_artifact(str(root), "t1", candidate_id="cand_2")
    assert art is not None and art.candidate.candidate_id == "cand_2"
    assert art.candidate.answer == "第二份候选"
    # final 仍是 cand_1
    art_final = standalone.load_artifact(str(root), "t1")
    assert art_final.candidate.candidate_id == "cand_1"


def test_discover_artifacts_candidate_filter(tmp_path):
    root = _write_run_root(tmp_path / "run")
    ep = root / "episodes" / "ep_1"
    with open(ep / "candidates.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"candidate_id": "cand_2", "answer": "b", "claims": [],
                            "evidence_pack_ids": ["pack_1"], "gaps": []}) + "\n")
    arts = standalone.discover_artifacts([str(root)], candidates=["cand_2"])
    assert [a.candidate.candidate_id for a in arts] == ["cand_2"]
    assert standalone.discover_artifacts([str(root)], candidates=["cand_missing"]) == []


def test_missing_pack_marks_U_zero_judge_calls(tmp_path, monkeypatch):
    """R3 反例一：候选声明了包但包缺失 → 该位置 U、原因入 basis、Judge 调用 0。"""
    root = _write_run_root(tmp_path / "run")
    ep = root / "episodes" / "ep_1"
    # 声明一个不存在的包
    cand = runner_load_candidate(ep, "cand_1")
    cand.evidence_pack_ids = ["pack_missing"]
    rewrite_candidate(ep, "cand_1", cand)
    art = standalone.load_artifact(str(root), "t1")
    assert art is not None
    assert art.scope_info.get("status") != "pack"  # pack_not_declared / missing_pack

    calls = {"n": 0}

    class CountingJudge:
        rubric = {"id": "x", "version": "1"}
        judge_ref = "j@v"
        def evaluate(self, *a, **k):
            calls["n"] += 1
            raise AssertionError("缺包时不得调用 Judge")

    v = standalone.judge_artifact(CountingJudge(), art, eval_run_id="e1")
    assert v.usable is False
    assert "pack_integrity" in v.basis
    assert calls["n"] == 0


def test_no_claim_candidate_with_missing_pack_also_U(tmp_path):
    """R3 反例二：无显式引用的候选 + 缺包 → 同样 U（曾在缺包时被照常送评）。"""
    root = _write_run_root(tmp_path / "run")
    ep = root / "episodes" / "ep_1"
    cand = runner_load_candidate(ep, "cand_1")
    cand.evidence_pack_ids = ["pack_missing"]
    cand.claims = []  # 无引用
    rewrite_candidate(ep, "cand_1", cand)
    art = standalone.load_artifact(str(root), "t1")
    assert art.scope_info.get("status") != "pack"

    class NoCall:
        rubric = {"id": "x", "version": "1"}
        judge_ref = "j@v"
        def evaluate(self, *a, **k):
            raise AssertionError("缺包时不得调用 Judge")

    v = standalone.judge_artifact(NoCall(), art, eval_run_id="e1")
    assert v.usable is False and "pack_integrity" in v.basis


def test_verifiable_empty_pack_still_judged(tmp_path):
    """显式冻结的可核验空包仍按原协议送评（不因空而 U）。"""
    root = _write_run_root(tmp_path / "run")
    ep = root / "episodes" / "ep_1"
    # 建一个合法空包
    materials = []
    pack_hash = pack_material_hash(materials)
    with open(ep / "packs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"pack_id": "pack_empty", "task_id": "t1", "materials": materials,
                            "gaps": [], "notes": [], "components": [],
                            "input_hash": pack_hash, "input_hash_v2": "he2"}) + "\n")
    cand = runner_load_candidate(ep, "cand_1")
    cand.evidence_pack_ids = ["pack_empty"]
    cand.claims = []
    rewrite_candidate(ep, "cand_1", cand)
    art = standalone.load_artifact(str(root), "t1")
    assert art.scope_info.get("status") == "pack"  # 空包但可核验 → 正常送评
    assert art.evidence == []


def test_resume_protocol_mismatch_not_reused(tmp_path):
    """R9：换判据（协议不同）→ 同候选重评是新记录，续跑不复用旧结果。"""
    prior = tmp_path / "prior.jsonl"
    prior.write_text(json.dumps({
        "task_id": "t1", "candidate_id": "cand_1", "usable": True,
        "protocol": {"rubric_id": "ogc", "rubric_version": "1", "prompt_sha256": "aaa"},
    }) + "\n", encoding="utf-8")
    same = standalone._load_done_keys([str(prior)], protocol={
        "rubric_id": "ogc", "rubric_version": "1", "prompt_sha256": "aaa"})
    diff = standalone._load_done_keys([str(prior)], protocol={
        "rubric_id": "ogc", "rubric_version": "2", "prompt_sha256": "aaa"})
    assert same == {("t1", "cand_1")}
    assert diff == set()  # 版本不同 → 不复用


def runner_load_candidate(ep, cid):
    return runner._load_final_candidate(str(ep), cid)


def rewrite_candidate(ep, cid, cand):
    rows = [json.loads(l) for l in open(ep / "candidates.jsonl", encoding="utf-8")]
    out = []
    for r in rows:
        if r.get("candidate_id") == cid:
            out.append({"candidate_id": cand.candidate_id, "answer": cand.answer,
                        "claims": [{"claim_id": c.claim_id, "text": c.text,
                                    "evidence_ids": list(c.evidence_ids)} for c in cand.claims],
                        "evidence_pack_ids": list(cand.evidence_pack_ids),
                        "gaps": list(cand.gaps), "fallback_render": False})
        else:
            out.append(r)
    with open(ep / "candidates.jsonl", "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
