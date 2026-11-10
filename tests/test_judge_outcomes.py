"""结果判定顺序与指标口径（手册 §15.3、§15.3.2）。"""

from __future__ import annotations

from framework.contracts import AnswerCandidate, EpisodeOutcome, Task, Verdict
from framework.judge import EpisodeEvaluation, compute_metrics, decide_outcome


def _verdict(cls: str | None, usable: bool = True) -> Verdict:
    return Verdict(
        verdict_id="v1",
        eval_run_id="e1",
        task_id="t1",
        answer_id="c1" if cls else None,
        rubric_version="v1",
        judge_ref="rule@v1",
        cls=cls,
        usable=usable,
    )


def _cand() -> AnswerCandidate:
    return AnswerCandidate(candidate_id="c1", answer="answer")


def test_no_answer_is_failure_not_unresolved():
    ev = decide_outcome(episode_id="ep1", task_id="t1", final_answer=None, verdict=None, failure_established=True)
    assert ev.outcome is EpisodeOutcome.FAILURE


def test_answer_without_verdict_is_unresolved():
    ev = decide_outcome(episode_id="ep1", task_id="t1", final_answer=_cand(), verdict=None, failure_established=False)
    assert ev.outcome is EpisodeOutcome.UNRESOLVED


def test_unusable_verdict_is_unresolved():
    v = _verdict(None, usable=False)
    ev = decide_outcome(episode_id="ep1", task_id="t1", final_answer=_cand(), verdict=v, failure_established=False)
    assert ev.outcome is EpisodeOutcome.UNRESOLVED


def test_class_c_is_failure():
    ev = decide_outcome(episode_id="ep1", task_id="t1", final_answer=_cand(), verdict=_verdict("C"), failure_established=False)
    assert ev.outcome is EpisodeOutcome.FAILURE


def test_class_d_is_success():
    ev = decide_outcome(episode_id="ep1", task_id="t1", final_answer=_cand(), verdict=_verdict("D"), failure_established=False)
    assert ev.outcome is EpisodeOutcome.SUCCESS


def test_insufficient_evidence_is_content_status_not_unresolved():
    """运行期的 INSUFFICIENT_EVIDENCE 不直接进入 U；U 只来自"没有可用判定"。"""
    ev = decide_outcome(
        episode_id="ep1",
        task_id="t1",
        final_answer=_cand(),
        verdict=_verdict("A"),
        failure_established=False,
    )
    assert ev.outcome is EpisodeOutcome.FAILURE


def test_metrics_keep_denominator_and_report_range():
    evs = [
        EpisodeEvaluation("e1", "t1", EpisodeOutcome.SUCCESS, _verdict("D"), "ok"),
        EpisodeEvaluation("e2", "t2", EpisodeOutcome.FAILURE, _verdict("A"), "no"),
        EpisodeEvaluation("e3", "t3", EpisodeOutcome.UNRESOLVED, None, "unscored"),
        EpisodeEvaluation("e4", "t4", EpisodeOutcome.UNRESOLVED, None, "unscored"),
    ]
    m = compute_metrics(evs)
    assert m["N"] == 4 and m["S"] == 1 and m["F"] == 1 and m["U"] == 2
    assert m["asr_point"] == 0.25
    assert m["eval_coverage"] == 0.5
    assert m["asr_range"] == [0.25, 0.75]


def test_class_dist_separates_unscored():
    evs = [
        EpisodeEvaluation("e1", "t1", EpisodeOutcome.SUCCESS, _verdict("D"), "ok"),
        EpisodeEvaluation("e2", "t2", EpisodeOutcome.UNRESOLVED, None, "unscored"),
    ]
    assert compute_metrics(evs)["class_dist"] == {"D": 1, "unscored": 1}


def test_gap_lines_do_not_count_as_coverage():
    from framework.contracts import AnswerCandidate
    from framework.judge import JudgeEval, Rubric

    rubric = Rubric(
        rubric_id="r",
        elements=[{"key": "库位", "critical": True, "accept": ["库位"]}, {"key": "登记", "critical": True, "accept": ["登记"]}],
    )
    judge = JudgeEval(rubric)
    task = Task(task_id="t", prompt="p", primary_dataset="synthetic")
    cand = AnswerCandidate(candidate_id="c", answer="- 未提供：库位、登记", gaps=["库位", "登记"])
    v = judge.evaluate(task, cand, "e1")
    assert v.coverage == 0.0
    assert v.cls == "A"
