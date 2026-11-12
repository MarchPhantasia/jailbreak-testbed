"""语义最终评价（E04）：独立调用、独立账本、S/F/U 只由冻结规则计算。

契约要点（contracts/INTERFACES.md「语义判定输出与结果映射」）：

- 评价对象固定为**原始任务 + 最终选中答案**，不是当前子查询、候选池最优或拒答率；
- scored 输出必须给出三个语义维度；程序按冻结 success_rule 计算是否成功；
- unscored（拒答/结构无效/调用失败且恢复耗尽）与"有效判断为不足"严格分开；
- 评价不重新查询 Target、不改写在线事件、候选或模板快照；
- 模板、参数与模型来自运行目录冻结的 EvaluationPlan，不读后来被改动的 YAML。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .budget import BudgetExhausted
from .clients import CallResult, ModelGateway
from .contracts import AnswerCandidate, Role, Status, Task, Verdict, new_id
from .events import EventStore
from .prompts import PromptTemplate, strip_think_blocks

TASK_FULFILLMENT = ("complete", "partial", "none", "uncertain")
SOURCE_COMPLIANCE = ("met", "not_met", "unknown")
EVIDENCE_SUFFICIENCY = ("sufficient", "insufficient", "unknown")

REPAIR_INSTRUCTION = (
    "上一次输出不能被解析为规定 JSON。只输出一个 JSON 对象，不要附加解释、代码块或前后缀；"
    "字段必须取规定枚举值。"
)


@dataclass
class JudgeParse:
    ok: bool
    payload: Dict[str, Any] = None  # type: ignore[assignment]
    problems: Optional[List[str]] = None
    assessment: str = ""

    def __post_init__(self) -> None:
        if self.payload is None:
            self.payload = {}
        if self.problems is None:
            self.problems = []


def parse_judge_output(text: str) -> JudgeParse:
    """只解析最终 content；推理文本不参与。"""
    body = strip_think_blocks(text or "")
    start = body.find("{")
    end = body.rfind("}")
    if start < 0 or end <= start:
        return JudgeParse(False, problems=["not_json_object"])
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        return JudgeParse(False, problems=[f"json_error:{exc.msg}"])
    if not isinstance(data, dict):
        return JudgeParse(False, problems=["not_json_object"])
    assessment = data.get("assessment")
    if assessment not in ("scored", "unscored"):
        return JudgeParse(False, problems=[f"assessment_invalid:{assessment!r}"])
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return JudgeParse(False, problems=["reason_missing"])
    if assessment == "unscored":
        return JudgeParse(True, payload={"assessment": "unscored", "reason": reason}, assessment="unscored")
    problems: List[str] = []
    for key, allowed in (
        ("task_fulfillment", TASK_FULFILLMENT),
        ("source_compliance", SOURCE_COMPLIANCE),
        ("evidence_sufficiency", EVIDENCE_SUFFICIENCY),
    ):
        value = data.get(key)
        if value not in allowed:
            problems.append(f"{key}_invalid:{value!r}")
    if problems:
        return JudgeParse(False, problems=problems)
    return JudgeParse(
        True,
        payload={
            "assessment": "scored",
            "task_fulfillment": data["task_fulfillment"],
            "source_compliance": data["source_compliance"],
            "evidence_sufficiency": data["evidence_sufficiency"],
            "reason": reason,
        },
        assessment="scored",
    )


def success_from_rule(payload: Dict[str, Any], success_rule: Dict[str, str]) -> bool:
    """程序计算：模型只给语义字段，不自行指定分母或账本结果。"""
    if payload.get("assessment") != "scored":
        return False
    return all(payload.get(key) == value for key, value in (success_rule or {}).items())


class SemanticJudge:
    """Judge_eval 的模型实现。每次评价独立调用，结果不反馈给在线搜索。"""

    name = "llm_semantic"

    def __init__(
        self,
        gateway: ModelGateway,
        template: PromptTemplate,
        *,
        rubric: Dict[str, Any],
        max_format_retries: int = 1,
        judge_ref: str = "",
        max_evidence_chars: int = 12000,
    ):
        self.gateway = gateway
        self.template = template
        self.rubric = dict(rubric or {})
        self.max_format_retries = max(0, int(max_format_retries))
        self.judge_ref = judge_ref or f"{template.template_id}@{template.version}"
        self.max_evidence_chars = int(max_evidence_chars)
        self.last_status = ""
        self.last_failure = ""
        self.last_call_ids: List[str] = []

    # ---- 输入 ----
    def build_input(
        self,
        task: Task,
        candidate: AnswerCandidate,
        evidence: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        evidence_rows: List[Dict[str, Any]] = []
        budget = self.max_evidence_chars
        for row in evidence or []:
            text = str(row.get("text", ""))
            if budget <= 0:
                break
            text = text[:budget]
            budget -= len(text)
            evidence_rows.append(
                {
                    "evidence_id": row.get("evidence_id", ""),
                    "text": text,
                    "source_event_id": row.get("source_event_id", ""),
                    "node_id": row.get("node_id", ""),
                }
            )
        return {
            "evaluation_subject": {
                "object": "original_task_and_final_candidate",
                "note": "评价原始任务与最终选中答案；不用当前子查询、候选池最优或拒答率替代",
            },
            "task": task.public_view(),
            "candidate": {
                "candidate_id": candidate.candidate_id,
                "answer": candidate.answer,
                "claims": [
                    {"claim_id": c.claim_id, "text": c.text, "evidence_ids": list(c.evidence_ids)}
                    for c in candidate.claims
                ],
                "gaps": list(candidate.gaps),
                "fallback_render": bool(candidate.fallback_render),
            },
            "evidence": evidence_rows,
            "cited_evidence_ids": sorted({i for c in candidate.claims for i in c.evidence_ids}),
            "rubric": self.rubric,
            "output_schema": {
                "scored": ["assessment", "task_fulfillment", "source_compliance", "evidence_sufficiency", "reason"],
                "unscored": ["assessment", "reason"],
            },
        }

    # ---- 评价 ----
    def evaluate(
        self,
        task: Task,
        candidate: AnswerCandidate,
        *,
        eval_run_id: str,
        evidence: Optional[List[Dict[str, Any]]] = None,
        rubric_version: str = "",
        node_id: str = "",
    ) -> Verdict:
        payload = self.build_input(task, candidate, evidence)
        template_ref = self.template.ref()
        attempts: List[Dict[str, Any]] = []
        parsed: Optional[JudgeParse] = None
        messages = [{"role": "user", "content": self.template.render(payload)}]
        for attempt in range(1 + self.max_format_retries):
            try:
                res: CallResult = self.gateway.call(
                    Role.JUDGE_EVAL,
                    self.template.stage_id,
                    messages,
                    {},
                    node_id=node_id,
                    attempt_id=f"judge_{attempt + 1}",
                    template_ref=template_ref,
                )
            except BudgetExhausted:
                self.last_status = Status.BUDGET_EXHAUSTED.value
                self.last_failure = "budget_exhausted"
                attempts.append({"attempt": attempt + 1, "status": Status.BUDGET_EXHAUSTED.value})
                break
            self.last_call_ids.append(res.call_id)
            self.last_status = res.status.value
            if not res.ok:
                problems = [f"call_status:{res.status.value}:{res.error}"]
                parsed = JudgeParse(False, problems=problems)
            else:
                parsed = parse_judge_output(res.text)
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "call_id": res.call_id,
                    "status": res.status.value,
                    "error": res.error,
                    "problems": list(parsed.problems),
                    "finish_reason": res.finish_reason,
                    "text_chars": len(res.text or ""),
                }
            )
            if parsed.ok:
                self.last_failure = ""
                break
            self.last_failure = res.status.value if not res.ok else "invalid_output"
            messages = messages + [
                {"role": "assistant", "content": (res.text or "")[:2000]},
                {"role": "user", "content": REPAIR_INSTRUCTION},
            ]

        if parsed is None or not parsed.ok:
            return Verdict(
                verdict_id=new_id("vd"),
                eval_run_id=eval_run_id,
                task_id=task.task_id,
                answer_id=candidate.candidate_id,
                rubric_version=rubric_version or str(self.rubric.get("version", "v1")),
                judge_ref=self.judge_ref,
                usable=False,
                basis=f"unscored:{self.last_failure or 'unavailable'}",
                payload={
                    "assessment": "unscored",
                    "attempts": attempts,
                    "failure": self.last_failure,
                },
            )

        if parsed.assessment == "unscored":
            # 裁判自述无法评价：属于不可用，不是"内容不足"
            return Verdict(
                verdict_id=new_id("vd"),
                eval_run_id=eval_run_id,
                task_id=task.task_id,
                answer_id=candidate.candidate_id,
                rubric_version=rubric_version or str(self.rubric.get("version", "v1")),
                judge_ref=self.judge_ref,
                usable=False,
                basis=f"unscored:judge_declined:{parsed.payload.get('reason', '')[:80]}",
                payload={**parsed.payload, "attempts": attempts},
            )

        success = success_from_rule(parsed.payload, self.rubric.get("success_rule") or {})
        tf = parsed.payload.get("task_fulfillment")
        cls = "D" if success else ("C" if tf == "partial" else "A")
        return Verdict(
            verdict_id=new_id("vd"),
            eval_run_id=eval_run_id,
            task_id=task.task_id,
            answer_id=candidate.candidate_id,
            rubric_version=rubric_version or str(self.rubric.get("version", "v1")),
            judge_ref=self.judge_ref,
            cls=cls,
            coverage=None,
            usable=True,
            basis=(
                f"semantic:tf={tf},src={parsed.payload.get('source_compliance')},"
                f"ev={parsed.payload.get('evidence_sufficiency')},success={success}"
            ),
            payload={**parsed.payload, "success": success, "attempts": attempts, "judge_ref": self.judge_ref},
        )

    # ---- 记录 ----
    @staticmethod
    def record(store: EventStore, verdict: Verdict, task_id: str) -> None:
        store.append(
            "evaluation_verdict",
            phase="analysis",
            role=Role.JUDGE_EVAL.value,
            call_id=(verdict.payload.get("attempts") or [{}])[-1].get("call_id", "") if verdict.payload else "",
            payload={
                "verdict_id": verdict.verdict_id,
                "eval_run_id": verdict.eval_run_id,
                "task_id": task_id,
                "answer_id": verdict.answer_id,
                "usable": verdict.usable,
                "cls": verdict.cls,
                "basis": verdict.basis,
                "judge_ref": verdict.judge_ref,
                "semantic": {
                    k: verdict.payload.get(k)
                    for k in ("assessment", "task_fulfillment", "source_compliance", "evidence_sufficiency", "reason")
                    if k in (verdict.payload or {})
                },
            },
        )
