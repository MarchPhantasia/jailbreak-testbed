"""装配与候选选择（手册 §10.1）。

约束：
- 材料来自 StateBackend 提供的 materials（原文行 / 摘要 / 命题），三种后端走同一路径；
- 缺项由要素关联计算（不是"要素名是否出现在答案里"）；
- 每条主张必须带 evidence_ids；缺项保留为缺项；
- 多候选共享同一证据包；选择器只读在线标准。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .budget import BudgetExhausted
from .contracts import (
    AgentState,
    AnswerCandidate,
    Claim,
    Fidelity,
    Origin,
    Role,
    Status,
    SupportStatus,
    Task,
    new_id,
)

#: 只归档、不进入本次装配的材料类型（手册 §5.2：归档用于复核，不是活跃输入）
ARCHIVED_KINDS = ("summary_archive",)
from .state import active_materials, compute_open, element_evidence, normalize_element


@dataclass
class EvidencePack:
    pack_id: str
    task_id: str
    materials: List[Dict[str, Any]] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def evidence_ids(self) -> List[str]:
        out: List[str] = []
        for m in self.materials:
            out.extend(m.get("evidence_ids") or [])
        return out

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class Assembler:
    name = "material_template"

    def build_pack(self, task: Task, state: AgentState, elements: Optional[List[Any]] = None) -> EvidencePack:
        gaps = [o["goal"] for o in compute_open(state, elements or [])]
        # 材料去重：同 key 同 value 只保留一次（不同要素同值仍各自保留）
        seen: set[Tuple[str, str]] = set()
        materials: List[Dict[str, Any]] = []
        for m in active_materials(state):
            if str(m.get("kind", "")) in ARCHIVED_KINDS:  # 双保险：归档永不进入装配
                continue
            sig = (str(m.get("key", "")), str(m.get("value", m.get("text", ""))))
            if sig in seen:
                continue
            seen.add(sig)
            materials.append(m)
        return EvidencePack(pack_id=new_id("pack"), task_id=task.task_id, materials=materials, gaps=gaps)

    def render(self, task: Task, pack: EvidencePack) -> Tuple[str, List[Claim], List[str]]:
        claims: List[Claim] = []
        lines: List[str] = []
        for m in pack.materials:
            key = str(m.get("key", "") or "")
            value = str(m.get("value", m.get("text", "")) or "")
            if not value:
                continue
            if key:
                lines.append(f"- {key}：{value}")
            elif str(m.get("kind", "")) == "raw_response":
                lines.append(value)  # 原文块保留段落与限定句，不强行压成一行
            else:
                lines.append(f"- {value}")
            claims.append(
                Claim(
                    claim_id=new_id("cl"),
                    text=value,
                    evidence_ids=list(m.get("evidence_ids") or []),
                    origin=_as_enum(Origin, m.get("origin"), Origin.TARGET_RESPONSE),
                    support_status=_as_enum(SupportStatus, m.get("support_status"), SupportStatus.UNKNOWN),
                    fidelity=_as_enum(Fidelity, m.get("fidelity"), Fidelity.VERBATIM),
                )
            )
        if pack.gaps:
            lines.append("- 未提供：" + "、".join(pack.gaps))
        return ("\n".join(lines) if lines else "（无可装配材料）"), claims, list(pack.gaps)

    def assemble(
        self,
        task: Task,
        state: AgentState,
        *,
        k: int = 1,
        elements: Optional[List[Any]] = None,
        stage_policy_ref: str = "",
    ) -> List[AnswerCandidate]:
        pack = self.build_pack(task, state, elements)
        return self.assemble_from_pack(task, pack, k=k, stage_policy_ref=stage_policy_ref)

    def assemble_from_pack(
        self,
        task: Task,
        pack: EvidencePack,
        *,
        k: int = 1,
        stage_policy_ref: str = "",
        node_id: str = "",
        view_id: str = "",
    ) -> List[AnswerCandidate]:
        """从已冻结的证据包生成候选；包不变则答案可重放。"""
        out: List[AnswerCandidate] = []
        for i in range(max(1, k)):
            answer, claims, gaps = self.render(task, pack)
            if i > 0:
                answer = answer + f"\n（排版变体 {i}）"
            out.append(
                AnswerCandidate(
                    candidate_id=new_id("cand"),
                    answer=answer,
                    claims=claims,
                    evidence_pack_ids=[pack.pack_id],
                    gaps=gaps,
                    assembly_config={"assembler": self.name, "k": k, "variant": i},
                    stage_policy_ref=stage_policy_ref,
                    end_state="assembled",
                )
            )
        return out


# --------------------------------------------------------------------------
# 模型装配（E03）：证据约束、引用校验、有限重试、明确降级
# --------------------------------------------------------------------------
ANSWER_CLAIM_CHECK = "line-ngram-evidence@v1"
#: 长于该长度的答案行必须能对上 claim 或证据包；更短的行按引导语/标题处理
COVERAGE_MIN_LINE_CHARS = 48
#: 行内 4-gram 至少这么多比例能在 claims+证据里找到，才算有依据
COVERAGE_MIN_OVERLAP = 0.30


def _looks_like_gap(line: str) -> bool:
    return bool(re.match(r"^\s*[-*]?\s*(未提供|未确认|缺项)", line))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _ngrams(text: str, n: int = 4) -> set:
    compact = _normalize(text)
    return {compact[i : i + n] for i in range(max(0, len(compact) - n + 1))}


def _line_overlap(line: str, reference: set) -> float:
    grams = _ngrams(line)
    if not grams:
        return 1.0
    return len(grams & reference) / len(grams)


def uncovered_ratio(answer: str, claims: List[Claim], evidence_text: str = "") -> Tuple[float, List[str]]:
    """答案实质段落是否有依据（结构检查，不证明语义支持）。

    - 参照物 = claims 文本 + 证据包原文：claim 写得"描述性"也允许，只要答案段落本身
      能在证据里找到对应内容；
    - 短行（< COVERAGE_MIN_LINE_CHARS）视为引导语/标题，不做覆盖判定；
    - 返回未被覆盖行的字符占比与行列表；普通 JSON 解析器不能据此宣称语义等价。
    """
    lines = [l.strip() for l in str(answer or "").splitlines()]
    body = [l for l in lines if l and not _looks_like_gap(l)]
    if not body:
        return 0.0, []
    reference = _ngrams(evidence_text)
    for c in claims:
        reference |= _ngrams(c.text)
    uncovered: List[str] = []
    substantive_chars = 0
    uncovered_chars = 0
    for line in body:
        weight = len(_normalize(line))
        if weight < COVERAGE_MIN_LINE_CHARS:
            continue
        substantive_chars += weight
        if _line_overlap(line, reference) < COVERAGE_MIN_OVERLAP:
            uncovered.append(line)
            uncovered_chars += weight
    if substantive_chars == 0:
        return 0.0, []
    return uncovered_chars / substantive_chars, uncovered


class LLMAssembler(Assembler):
    """把冻结证据包交给 Assembler 角色模型，产出 answer/claims/gaps。

    校验只做结构与引用归属：引用 ID 属于该包、schema 字段合法、答案实质行能被 claims
    覆盖。语义支持与保真度仍为未核验，不在这里自证。
    """

    def __init__(
        self,
        gateway,
        template,
        *,
        max_format_retries: int = 1,
        max_uncovered_ratio: float = 0.25,
        stage_id: str = "assemble",
        output_limit: int = 20000,
    ):
        self.gateway = gateway
        self.template = template
        self.max_format_retries = max(0, int(max_format_retries))
        self.max_uncovered_ratio = float(max_uncovered_ratio)
        self.stage_id = stage_id
        self.output_limit = int(output_limit)
        self.name = "llm_assembler"
        self.last_call_ids: List[str] = []
        self.last_failure = ""

    # ---- 输入 ----
    def _input_payload(self, task: Task, pack: EvidencePack) -> Dict[str, Any]:
        return {
            "task": task.public_view(),
            "pack": {
                "pack_id": pack.pack_id,
                "materials": pack.materials,
                "gaps": pack.gaps,
                "notes": pack.notes,
                "evidence_ids": pack.evidence_ids(),
            },
            "constraints": {
                "must_keep_conditions": True,
                "no_world_knowledge": True,
                "unknown_stays_unknown": True,
                "output": "answer/claims/gaps",
            },
        }

    # ---- 解析与校验 ----
    @staticmethod
    def _extract_json(text: str) -> Optional[Dict[str, Any]]:
        from .prompts import strip_think_blocks

        body = strip_think_blocks(text)
        start = body.find("{")
        end = body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _validate(self, data: Optional[Dict[str, Any]], pack: EvidencePack) -> Tuple[Optional[Dict[str, Any]], List[str]]:
        problems: List[str] = []
        if not isinstance(data, dict):
            return None, ["not_json_object"]
        answer = data.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            problems.append("answer_missing_or_empty")
        elif len(answer) > self.output_limit:
            problems.append("answer_too_long")
        claims_raw = data.get("claims")
        if not isinstance(claims_raw, list) or not claims_raw:
            problems.append("claims_missing")
            claims_raw = []
        gaps_raw = data.get("gaps")
        if gaps_raw is None:
            gaps_raw = []
        if not isinstance(gaps_raw, list) or any(not isinstance(g, str) for g in gaps_raw):
            problems.append("gaps_not_list_of_string")
            gaps_raw = []

        valid_ids = set(pack.evidence_ids())
        claims: List[Claim] = []
        seen_claim_ids: set = set()
        for i, raw in enumerate(claims_raw):
            if not isinstance(raw, dict):
                problems.append(f"claim_{i}_not_object")
                continue
            cid = raw.get("claim_id")
            text = raw.get("text")
            ids = raw.get("evidence_ids")
            if not isinstance(cid, str) or not cid.strip():
                problems.append(f"claim_{i}_bad_id")
                continue
            if cid in seen_claim_ids:
                problems.append(f"claim_{i}_duplicate_id")
                continue
            if not isinstance(text, str) or not text.strip():
                problems.append(f"claim_{i}_bad_text")
                continue
            if not isinstance(ids, list) or not ids or any(not isinstance(x, str) or not x for x in ids):
                problems.append(f"claim_{i}_bad_evidence_ids")
                continue
            if len(set(ids)) != len(ids):
                problems.append(f"claim_{i}_duplicate_evidence_ids")
                continue
            unknown = [x for x in ids if x not in valid_ids]
            if unknown:
                # N12：不存在/不属于本证据包的引用整体拒收，而不是删掉引用保留无出处句子
                problems.append(f"claim_{i}_unknown_evidence:{','.join(unknown[:4])}")
                continue
            seen_claim_ids.add(cid)
            claims.append(Claim(claim_id=cid, text=text, evidence_ids=list(ids), origin=Origin.TARGET_RESPONSE))

        if not problems and not claims:
            problems.append("no_valid_claims")
        if not problems:
            pack_text = "\n".join(
                str(m.get("value", m.get("text", ""))) for m in pack.materials if m.get("value") or m.get("text")
            )
            ratio, uncovered = uncovered_ratio(answer, claims, pack_text)
            if ratio > self.max_uncovered_ratio:
                problems.append(f"answer_lines_not_covered:{len(uncovered)}")
        if problems:
            return None, problems
        return {"answer": answer, "claims": claims, "gaps": list(gaps_raw)}, []

    # ---- 装配 ----
    def assemble_from_pack(self, task: Task, pack: EvidencePack, *, k: int = 1, stage_policy_ref: str = "", node_id: str = "", view_id: str = "") -> List[AnswerCandidate]:
        payload = self._input_payload(task, pack)
        template_ref = self.template.ref() if hasattr(self.template, "ref") else {}
        attempts: List[Dict[str, Any]] = []
        parsed: Optional[Dict[str, Any]] = None
        last_status = ""
        for attempt in range(1 + self.max_format_retries):
            rendered = self.template.render(payload)
            try:
                res = self.gateway.call(
                    Role.ASSEMBLER,
                    self.stage_id,
                    [{"role": "user", "content": rendered}],
                    {},
                    node_id=node_id,
                    view_id=view_id,
                    attempt_id=f"assemble_{attempt + 1}",
                    template_ref=template_ref,
                    input_evidence_ids=pack.evidence_ids(),
                )
            except BudgetExhausted:
                self.last_failure = "budget_exhausted"
                attempts.append({"attempt": attempt + 1, "status": "BUDGET_EXHAUSTED"})
                break
            self.last_call_ids.append(res.call_id)
            last_status = res.status.value
            data, problems = (None, ["not_dispatched"])
            if res.ok:
                data, problems = self._validate(self._extract_json(res.text), pack)
            else:
                problems = [f"call_status:{res.status.value}:{res.error}"]
            attempts.append(
                {
                    "attempt": attempt + 1,
                    "call_id": res.call_id,
                    "status": res.status.value,
                    "error": res.error,
                    "problems": problems,
                    "text_chars": len(res.text or ""),
                }
            )
            if data is not None:
                parsed = data
                break
        self.last_failure = "" if parsed else (last_status or "invalid_output")

        if parsed is None:
            return self._fallback(task, pack, stage_policy_ref, attempts, k=k)

        claims = parsed["claims"]
        pack_text = "\n".join(
            str(m.get("value", m.get("text", ""))) for m in pack.materials if m.get("value") or m.get("text")
        )
        ratio, _ = uncovered_ratio(parsed["answer"], claims, pack_text)
        candidate = AnswerCandidate(
            candidate_id=new_id("cand"),
            answer=parsed["answer"],
            claims=claims,
            evidence_pack_ids=[pack.pack_id],
            gaps=parsed["gaps"],
            assembly_config={
                "assembler": self.name,
                "k": k,
                "template_id": template_ref.get("template_id", ""),
                "template_sha256": template_ref.get("sha256", ""),
                "answer_claim_check": ANSWER_CLAIM_CHECK,
                "uncovered_ratio": round(ratio, 4),
                "attempts": attempts,
            },
            stage_policy_ref=stage_policy_ref,
            processing_refs=[f"assembler:{cid}" for cid in self.last_call_ids if cid] + [f"pack:{pack.pack_id}"],
            fallback_render=False,
            end_state="assembled",
        )
        return [candidate]

    def _fallback(self, task: Task, pack: EvidencePack, stage_policy_ref: str, attempts: List[Dict[str, Any]], *, k: int = 1) -> List[AnswerCandidate]:
        """声明的原文回退：保留 Target 原文与缺项，明确标记 fallback_render。"""
        answer, claims, gaps = self.render(task, pack)
        return [
            AnswerCandidate(
                candidate_id=new_id("cand"),
                answer=answer,
                claims=claims,
                evidence_pack_ids=[pack.pack_id],
                gaps=list(gaps),
                assembly_config={
                    "assembler": "material_template",
                    "fallback_of": self.name,
                    "k": k,
                    "attempts": attempts,
                    "failure": self.last_failure,
                },
                stage_policy_ref=stage_policy_ref,
                processing_refs=[f"fallback:material_template@v1", f"pack:{pack.pack_id}"],
                fallback_render=True,
                end_state="assembled_fallback",
            )
        ]


def make_assembler(cfg: Dict[str, Any], *, gateway=None, template=None):
    """按 method.assembly.type 选择装配器；llm 必须真正拿到网关与模板。"""
    method = cfg.get("method") or {}
    kind = str(((method.get("assembly") or {}).get("type")) or "material_template").lower()
    if kind == "material_template":
        return Assembler()
    if kind == "llm":
        if gateway is None or template is None:
            raise ValueError("assembly.type=llm 需要 Assembler 网关与冻结模板")
        spec = method.get("assembly") or {}
        return LLMAssembler(
            gateway,
            template,
            max_format_retries=int(spec.get("max_format_retries", 1)),
            max_uncovered_ratio=float(spec.get("max_uncovered_ratio", 0.25)),
        )
    raise ValueError(f"未支持的 assembly.type：{kind}（不做隐式回退）")


def _as_enum(enum_cls, value, default):
    if value is None or value == "":
        return default
    try:
        return enum_cls(value)
    except ValueError:
        return default


class CandidateSelector:
    name = "first"

    def select(self, candidates: List[AnswerCandidate], scores: Optional[Dict[str, float]] = None) -> Optional[AnswerCandidate]:
        if not candidates:
            return None
        if scores:
            return max(candidates, key=lambda c: scores.get(c.candidate_id, -1.0))
        return candidates[0]


class CoverageSelector(CandidateSelector):
    name = "coverage"

    def select(self, candidates: List[AnswerCandidate], scores: Optional[Dict[str, float]] = None):
        if not candidates:
            return None

        def key(c: AnswerCandidate) -> int:
            return sum(1 for cl in c.claims if cl.evidence_ids) - len(c.gaps)

        return max(candidates, key=key)


def make_selector(name: str) -> CandidateSelector:
    return CoverageSelector() if (name or "").lower() == "coverage" else CandidateSelector()


__all__ = [
    "Assembler",
    "LLMAssembler",
    "CandidateSelector",
    "CoverageSelector",
    "EvidencePack",
    "make_assembler",
    "make_selector",
    "compute_open",
    "element_evidence",
    "normalize_element",
]
