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
    Phase,
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
    #: 控制器声明的答案组件清单（plan.v6 覆盖矩阵输入；随包冻结）
    components: List[Dict[str, Any]] = field(default_factory=list)

    def evidence_ids(self) -> List[str]:
        out: List[str] = []
        for m in self.materials:
            out.extend(m.get("evidence_ids") or [])
        return out

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def raw_citation_lines(materials: List[Dict[str, Any]]) -> Dict[str, Optional[Tuple[str, Origin]]]:
    """Index the visible lines of raw replies whose nonblank lines align with their IDs."""
    lines = {}
    for material in materials:
        if material.get("kind") != "raw_response":
            continue
        value = material.get("value", material.get("text"))
        ids = material.get("evidence_ids") or []
        if not isinstance(value, str) or not isinstance(ids, list):
            continue
        visible = [line for line in value.splitlines() if line.strip()]
        if len(visible) != len(ids):
            continue
        declared = material.get("origin", Origin.TARGET_RESPONSE)
        origin = _as_enum(Origin, declared, Origin.UNKNOWN)
        for eid, line in zip(ids, visible):
            if not isinstance(eid, str):
                continue
            source = (line, origin)
            lines[eid] = source if lines.get(eid, source) == source else None
    return lines


def record_model_claim(claim_id: str, text: str, ids: List[str],
                       raw_lines: Dict[str, Optional[Tuple[str, Origin]]]) -> Claim:
    """A matching cited line establishes literal wording. Semantic support is judged separately."""
    origins = {source[1] for eid in ids if (source := raw_lines.get(eid)) and source[0] == text}
    return Claim(
        claim_id=claim_id, text=text, evidence_ids=list(ids), text_producer="llm_assembler",
        origin=next(iter(origins)) if len(origins) == 1 else Origin.UNKNOWN,
        fidelity=Fidelity.VERBATIM if origins else Fidelity.UNKNOWN,
    )


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
        return EvidencePack(
            pack_id=new_id("pack"),
            task_id=task.task_id,
            materials=materials,
            gaps=gaps,
            components=[dict(c) for c in (getattr(state, "components", []) or [])],
        )

    def render(self, task: Task, pack: EvidencePack) -> Tuple[str, List[Claim], List[str]]:
        claims: List[Claim] = []
        lines: List[str] = []
        for m in pack.materials:
            key = str(m.get("key", "") or "")
            value = str(m.get("value", m.get("text", "")) or "")
            if not value:
                continue
            raw = m.get("kind") == "raw_response"
            if key:
                lines.append(f"- {key}：{value}")
            elif raw:
                lines.append(value)  # 原文块保留段落与限定句，不强行压成一行
            else:
                lines.append(f"- {value}")
            claims.append(
                Claim(
                    claim_id=new_id("cl"),
                    text=value,
                    evidence_ids=list(m.get("evidence_ids") or []),
                    origin=_as_enum(Origin, m.get("origin"), Origin.TARGET_RESPONSE if raw else Origin.UNKNOWN),
                    support_status=_as_enum(SupportStatus, m.get("support_status"), SupportStatus.UNKNOWN),
                    fidelity=_as_enum(Fidelity, m.get("fidelity"), Fidelity.VERBATIM if raw else Fidelity.UNKNOWN),
                    text_producer="material_template",
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
ANSWER_CLAIM_CHECK = "answer-evidence-ngram@v1"
DIAGNOSTICS_VERSION = "structural@v1"
#: 诊断版本（两种 lexical_policy 都记录，便于对照）
LEXICAL_DIAGNOSTIC_VERSION = "answer-evidence-ngram@v1"
#: 字面覆盖策略：diagnostic_only = 只诊断（默认）；hard_gate = 旧行为，低覆盖触发重试/降级
LEXICAL_POLICY_VERSION = "lexical-policy@v1"
LEXICAL_POLICIES = ("diagnostic_only", "hard_gate")
DEFAULT_LEXICAL_POLICY = "diagnostic_only"
#: 长于该长度的答案行必须能对上 claim 或证据包；更短的行按引导语/标题处理
COVERAGE_MIN_LINE_CHARS = 48
#: 行内 4-gram 至少这么多比例能在 claims+证据里找到，才算有依据
COVERAGE_MIN_OVERLAP = 0.30


def resolve_lexical_policy(value: Any) -> str:
    """解析 method.assembly.lexical_policy；未知值直接报错，不隐式回退。"""
    if value is None or value == "":
        return DEFAULT_LEXICAL_POLICY
    text = str(value).strip().lower()
    if text not in LEXICAL_POLICIES:
        raise ValueError(f"method.assembly.lexical_policy 只能是 {' / '.join(LEXICAL_POLICIES)}：{value!r}")
    return text


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
    """答案实质段落是否与**证据原文**对得上（结构检查，不证明语义支持）。

    - 给了 evidence_text 就只参照证据原文：把材料外内容同时写进 claim 不能洗白答案（R4）；
    - 没给 evidence_text 时退回参照 claims（仅供旧调用/诊断对照）；
    - 短行（< COVERAGE_MIN_LINE_CHARS）视为引导语/标题，不做判定并单独计数；
    - 该指标只说明"结构上能否找到对应文字"，不得当成来源或保真结论。
    """
    lines = [l.strip() for l in str(answer or "").splitlines()]
    body = [l for l in lines if l and not _looks_like_gap(l)]
    if not body:
        return 0.0, []
    if evidence_text:
        reference = _ngrams(evidence_text)
    else:
        reference = set()
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


CITATION_RELOCATION_VERSION = "citation-relocate@v1"

_META_ROW_PATTERNS = (
    # 分隔线 / 标题行 / 短标签行
    "separator", "heading", "label",
    # 拒答或告诫前言行（内容本身不含对主张的支持）
    "refusal_preamble",
)


def _row_kind(text: str) -> str:
    """判断一条证据行的形态：separator/heading/label/refusal_preamble 或 substantive。

    只做形态判断，不做语义判断——落在这四种形态上的引用都可能是"引用定位错误"：
    主张的实质支持通常在同材料的相邻行（Frequency evidence/Operation 等正文行）。
    """
    t = (text or "").strip()
    if not t:
        return "separator"
    if t.strip("-*_ #") == "" or set(t) <= set("-_*# "):
        return "separator"
    if t.lstrip().startswith("#"):
        return "heading"
    import re
    # 编号/加粗短标签（≤64 字符、无句号结尾的标题式行）
    if len(t) <= 64 and not t.endswith((".", "!", "?", "。", "！", "？")) and (
            re.match(r"^\*?\*?\d+\.\s*\*\*", t) or t.startswith("**")):
        return "label"
    # 拒答/告诫前言行：开头即元评论而非事实内容
    if re.match(
        r"^(\d+\.\s*)?(\*\*)?(I can('|no)t|I won('|no)t|Two caveats|Caveats? first|"
        r"This one, unlike|Same answer shape|I have no access|I don('|no)t have|"
        r"To be clear|The data problem|The deeper problem|The substantive problem)",
        t, re.IGNORECASE):
        return "refusal_preamble"
    return "substantive"


def relocate_meta_citations(claims, materials, evidence_text_by_id, *, dry_run: bool = False):
    """引用定位修复：把落在元行（分隔线/标题/标签/拒答前言）上的引用扩宽为
    所在材料的完整证据跨度。

    动机（R22 回归发现）：模型常把引用落在材料开头的告诫/拒答前言行或标题行上，
    而主张的实质支持在同材料后续正文行。扩宽到材料级跨度后引用必然覆盖支持行
    （代价是精度下降——这是有意的：宁全勿错引）。答案正文与主张文本不变。

    - materials：包材料（取 evidence_ids 跨度）；
    - evidence_text_by_id：evidence.jsonl 的 {evidence_id: 行文本}（做形态判断）。
    返回 (claims, report)：report 记录每处 from→to，供落盘追溯。
    """
    span_by_id = {}
    for m in materials:
        ids = list(m.get("evidence_ids") or [])
        for eid in ids:
            span_by_id.setdefault(eid, ids)
    report = []

    def _widen(c, eid, kind):
        span = span_by_id[eid]
        before = list(new_ids)
        added = [x for x in span if x not in new_ids]
        if added:
            report.append({"claim_id": c.claim_id, "from": [eid], "kind": kind,
                           "to": list(span)})
            new_ids.extend(added)
        else:
            new_ids.append(eid)

    for c in claims:
        new_ids = []
        # 层1：元行（分隔线/标题/标签/拒答前言）引用 → 材料完整跨度
        for eid in c.evidence_ids:
            kind = _row_kind(evidence_text_by_id.get(eid, ""))
            if kind != "substantive" and eid in span_by_id:
                _widen(c, eid, kind)
            elif eid not in new_ids:
                new_ids.append(eid)
        # 层2：多行材料上的**单行**引用 → 材料完整跨度。依据 R22 复评：模型常在
        # 正确材料里挑一行"代表行"（引言句/机制概述行），而主张实据在同材料其他行；
        # 单行引用扩宽后必含实据（B 模板按条件引材料跨度即被判 clean）。
        # 引用 2+ 行的主张视为已做主题选择，保持不动。
        if len(c.evidence_ids) == 1:
            (eid,) = c.evidence_ids
            span = span_by_id.get(eid)
            if span and len(span) > 1 and eid in span:
                _widen(c, eid, "single_row_in_multirow_material")
        if not dry_run:
            c.evidence_ids = new_ids
    return claims, report


def coverage_diagnostics(
    answer: str,
    claims: List[Claim],
    pack_text: str,
    evidence_text_by_id: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """三类结构关系的分开诊断（R4）：答案-证据、答案-claim、claim-引用对齐。

    都只是字符/ngram 层面的结构指标，`structural_only=True`；语义支持、保真与真实性
    仍由 Judge_eval/人工复核判定，不能用这里的数字宣称"有依据"。
    """
    lines = [l.strip() for l in str(answer or "").splitlines()]
    body = [l for l in lines if l and not _looks_like_gap(l)]
    short = [l for l in body if len(_normalize(l)) < COVERAGE_MIN_LINE_CHARS]
    evidence_ratio, unsupported = uncovered_ratio(answer, [], pack_text)
    claim_ref = set()
    for c in claims:
        claim_ref |= _ngrams(c.text)
    claim_covered = 0
    for line in body:
        if len(_normalize(line)) < COVERAGE_MIN_LINE_CHARS:
            continue
        if _line_overlap(line, claim_ref) >= COVERAGE_MIN_OVERLAP:
            claim_covered += 1
    long_lines = max(0, len(body) - len(short))
    aligned = 0
    for c in claims:
        # 逐 claim：与它引用的证据文本比较；没有单条映射时退化为与包原文比较
        ref: set = set()
        for eid in c.evidence_ids:
            ref |= _ngrams((evidence_text_by_id or {}).get(eid, ""))
        if not ref:
            ref = _ngrams(pack_text)
        if _line_overlap(c.text, ref) >= COVERAGE_MIN_OVERLAP:
            aligned += 1
    return {
        "version": DIAGNOSTICS_VERSION,
        "structural_only": True,
        "answer_evidence": {
            "uncovered_ratio": round(evidence_ratio, 4),
            "unsupported_lines": len(unsupported),
            "exempt_short_lines": len(short),
        },
        "answer_claim": {
            "long_lines": long_lines,
            "covered_long_lines": claim_covered,
            "coverage_ratio": round(claim_covered / long_lines, 4) if long_lines else None,
        },
        "claim_citation": {
            "claims": len(claims),
            "claims_overlapping_pack_text": aligned,
            "aligned_ratio": round(aligned / len(claims), 4) if claims else None,
        },
    }


class LLMAssembler(Assembler):
    """把冻结证据包交给 Assembler 角色模型，产出 answer/claims/gaps。

    校验分两层（W03）：

    - 结构层（两种策略都执行）：answer 非空且长度合法、claims/gaps schema 合法、引用 ID
      存在且属于冻结包、无重复 ID；语义支持与保真度仍交给冻结语义 Judge_eval，不在这里自证。
    - 字面层：``lexical_policy=diagnostic_only``（默认）只记录 answer—evidence 4-gram 覆盖率；
      ``hard_gate`` 保留旧行为，低覆盖触发格式重试或原文降级（明确限制跨语言/改写）。
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
        lexical_policy: str = DEFAULT_LEXICAL_POLICY,
        relocate_meta_rows: bool = True,
        row_text_by_id=None,
        phase: Phase = Phase.ONLINE,
    ):
        self.gateway = gateway
        self.template = template
        self.phase = Phase(phase)
        # 引用定位修复（citation-relocate@v1）：元行引用扩宽为材料完整跨度。
        # row_text_by_id 由正式入口注入 evidence.jsonl 的行级文本；缺省时跳过。
        self.relocate_meta_rows = bool(relocate_meta_rows)
        self.row_text_by_id = dict(row_text_by_id or {})
        self.max_format_retries = max(0, int(max_format_retries))
        self.max_uncovered_ratio = float(max_uncovered_ratio)
        self.stage_id = stage_id
        self.output_limit = int(output_limit)
        self.name = "llm_assembler"
        self.lexical_policy = resolve_lexical_policy(lexical_policy)
        self.last_call_ids: List[str] = []
        self.last_failure = ""
        self.last_lexical_diagnostics: Dict[str, Any] = {}

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
                # 控制器声明的组件清单（可能为空=未声明）：装配时判定 direct/adjacent/missing 覆盖
                "components": list(pack.components or []),
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
        from .prompts import final_content

        body, _meta = final_content(text)
        if not body:
            return None
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
        raw_lines = raw_citation_lines(pack.materials)
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
            claims.append(record_model_claim(cid, text, ids, raw_lines))

        if not problems and not claims:
            problems.append("no_valid_claims")
        if not problems:
            pack_text = "\n".join(
                str(m.get("value", m.get("text", ""))) for m in pack.materials if m.get("value") or m.get("text")
            )
            ratio, unsupported = uncovered_ratio(answer, [], pack_text)
            self.last_lexical_diagnostics = {
                "version": LEXICAL_DIAGNOSTIC_VERSION,
                "uncovered_ratio": round(ratio, 4),
                "unsupported_lines": len(unsupported),
                "gate": self.lexical_policy,
                "threshold": self.max_uncovered_ratio if self.lexical_policy == "hard_gate" else None,
                "triggered": bool(self.lexical_policy == "hard_gate" and ratio > self.max_uncovered_ratio),
            }
            if self.lexical_policy == "hard_gate" and ratio > self.max_uncovered_ratio:
                # 旧行为（研究变体）：低字面覆盖视为格式问题，触发有限重试或原文降级
                problems.append(f"answer_not_grounded_in_pack:{len(unsupported)}")
        else:
            self.last_lexical_diagnostics = {
                "version": LEXICAL_DIAGNOSTIC_VERSION,
                "uncovered_ratio": None,
                "unsupported_lines": None,
                "gate": self.lexical_policy,
                "threshold": self.max_uncovered_ratio if self.lexical_policy == "hard_gate" else None,
                "triggered": False,
                "note": "结构问题优先：未计算字面诊断",
            }
        if problems:
            return None, problems
        return {"answer": answer, "claims": claims, "gaps": list(gaps_raw)}, []

    # ---- 装配 ----
    def _reset_call_state(self) -> None:
        """每次装配从干净状态开始（X02）：诊断、失败状态与调用引用不得跨调用沿用。"""
        self.last_call_ids = []
        self.last_failure = ""
        self.last_lexical_diagnostics = {}

    def _not_computed_diagnostics(self, reason: str) -> Dict[str, Any]:
        """本次没有可校验的模型正文时的诊断：明确"未计算"，不用 0 冒充"没有未覆盖部分"。"""
        return {
            "version": LEXICAL_DIAGNOSTIC_VERSION,
            "uncovered_ratio": None,
            "unsupported_lines": None,
            "gate": self.lexical_policy,
            "threshold": self.max_uncovered_ratio if self.lexical_policy == "hard_gate" else None,
            "triggered": False,
            "note": f"本次没有可校验的模型正文（{reason}）；字面诊断未计算",
        }

    def _generate_sample(
        self,
        task: Task,
        pack: EvidencePack,
        *,
        sample_index: int,
        cache_bypass: bool,
        stage_policy_ref: str,
        node_id: str,
        view_id: str,
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], bool]:
        """生成一个候选样本：独立请求 + 各自格式重试。

        返回 (解析结果, 本次样本的 attempts, 预算是否耗尽)。样本之间互不共享调用；
        一个样本失败不中止其余样本（C08）。
        """
        payload = self._input_payload(task, pack)
        template_ref = self.template.ref() if hasattr(self.template, "ref") else {}
        attempts: List[Dict[str, Any]] = []
        parsed: Optional[Dict[str, Any]] = None
        last_status = ""
        budget_out = False
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
                    attempt_id=f"assemble_s{sample_index}_{attempt + 1}",
                    phase=self.phase,
                    template_ref=template_ref,
                    input_evidence_ids=pack.evidence_ids(),
                    cache_bypass=cache_bypass,
                )
            except BudgetExhausted:
                self.last_failure = "budget_exhausted"
                attempts.append({"sample": sample_index, "attempt": attempt + 1, "status": "BUDGET_EXHAUSTED"})
                budget_out = True
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
                    "sample": sample_index,
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
        if parsed is None and not budget_out:
            self.last_failure = last_status or "invalid_output"
        return parsed, attempts, budget_out

    def assemble_from_pack(self, task: Task, pack: EvidencePack, *, k: int = 1, stage_policy_ref: str = "", node_id: str = "", view_id: str = "") -> List[AnswerCandidate]:
        """在同一个冻结证据包上生成至多 k 个候选（C08）。

        - 每个样本是独立真实请求（k>1 时绕过响应缓存），失败/重试逐样本记录；
        - 预算中途耗尽时保留已生成样本；全部失败走既定原文回退；
        - 两个样本产出相同答案是真实重复调用的事实，保留候选并标注 duplicate_of_sample，
          不删除也不冒充不同内容；候选择优由冻结在线标准负责，本层不做语义排序。
        """
        self._reset_call_state()
        k = max(1, int(k))
        template_ref = self.template.ref() if hasattr(self.template, "ref") else {}
        all_attempts: List[Dict[str, Any]] = []
        candidates: List[AnswerCandidate] = []
        seen_answers: Dict[str, int] = {}
        budget_out = False
        for sample in range(1, k + 1):
            parsed, attempts, budget_out = self._generate_sample(
                task,
                pack,
                sample_index=sample,
                cache_bypass=k > 1,
                stage_policy_ref=stage_policy_ref,
                node_id=node_id,
                view_id=view_id,
            )
            all_attempts.extend(attempts)
            # R02：首次预算耗尽立即结束本轮采样；不得再尝试后续样本
            if budget_out:
                break
            if parsed is None:
                continue
            self.last_failure = ""
            claims = parsed["claims"]
            pack_text = "\n".join(
                str(m.get("value", m.get("text", ""))) for m in pack.materials if m.get("value") or m.get("text")
            )
            evidence_text_by_id: Dict[str, str] = {}
            for m in pack.materials:
                text = str(m.get("value", m.get("text", "")))
                for eid in m.get("evidence_ids") or []:
                    evidence_text_by_id.setdefault(eid, text)
            diagnostics = coverage_diagnostics(parsed["answer"], claims, pack_text, evidence_text_by_id)
            ratio = diagnostics["answer_evidence"]["uncovered_ratio"]
            dup_of = seen_answers.get(parsed["answer"])
            seen_answers.setdefault(parsed["answer"], sample)
            candidates.append(
                AnswerCandidate(
                    candidate_id=new_id("cand"),
                    answer=parsed["answer"],
                    claims=claims,
                    evidence_pack_ids=[pack.pack_id],
                    gaps=parsed["gaps"],
                    assembly_config={
                        "assembler": self.name,
                        "k": k,
                        "sample_index": sample,
                        **({"duplicate_of_sample": dup_of} if dup_of is not None else {}),
                        "template_id": template_ref.get("template_id", ""),
                        "template_sha256": template_ref.get("sha256", ""),
                        "answer_claim_check": ANSWER_CLAIM_CHECK,
                        "uncovered_ratio": round(ratio, 4),
                        "diagnostics": diagnostics,
                        "lexical_policy": self.lexical_policy,
                        "lexical_policy_version": LEXICAL_POLICY_VERSION,
                        "lexical_diagnostics": dict(self.last_lexical_diagnostics),
                        "attempts": attempts,
                    },
                    stage_policy_ref=stage_policy_ref,
                    processing_refs=[f"assembler:{cid}" for cid in (a.get("call_id") for a in attempts) if cid]
                    + [f"pack:{pack.pack_id}"],
                    fallback_render=False,
                    end_state="assembled",
                )
            )
            if budget_out:
                break
        if budget_out and candidates:
            # 预算中途耗尽：保留已生成样本，如实记录未完成的采样数
            candidates[-1].assembly_config["samples_completed"] = len(candidates)
            candidates[-1].assembly_config["sampling_interrupted"] = True
        # R03：整轮采样记录与成功候选分开保存——关联 pack/样本索引/attempt/call_id/
        # 网关状态与解析校验问题；成功候选只引用自己的生成调用，不吸收失败样本的调用
        self._record_samples_event(pack, node_id, view_id, k, all_attempts, candidates)
        if not candidates:
            self.last_failure = self.last_failure or "invalid_output"
            return self._fallback(task, pack, stage_policy_ref, all_attempts, k=k)
        return candidates

    def _record_samples_event(self, pack, node_id, view_id, k, all_attempts, candidates) -> None:
        store = getattr(self.gateway, "store", None)
        if store is None:
            return
        store.append(
            "assembly_samples",
            phase=self.phase,
            role="assembler",
            stage_id=self.stage_id,
            node_id=node_id,
            view_id=view_id,
            payload={
                "pack_id": pack.pack_id,
                "k": k,
                "samples": list(all_attempts),
                "candidates": [c.candidate_id for c in candidates],
                "fallback": not candidates,
            },
        )

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
                    "lexical_policy": self.lexical_policy,
                    "lexical_policy_version": LEXICAL_POLICY_VERSION,
                    # 回退候选不是"模型输出"：本次没有可校验正文，字面诊断明确记为未计算，
                    # 不能把上一次装配（或其他包）的诊断挂到这条候选上（X02）
                    "lexical_diagnostics": self._not_computed_diagnostics(self.last_failure or "invalid_output"),
                    "diagnostics_subject": "fallback_render",
                },
                stage_policy_ref=stage_policy_ref,
                processing_refs=[f"fallback:material_template@v1", f"pack:{pack.pack_id}"],
                fallback_render=True,
                end_state="assembled_fallback",
            )
        ]


def make_assembler(cfg: Dict[str, Any], *, gateway=None, template=None, evidence_rows=None):
    """按 method.assembly.type 选择装配器；llm 必须真正拿到网关与模板。

    evidence_rows：episode 的 evidence.jsonl 行列表（dict）。llm 装配器用它注入
    行级文本，启用 citation-relocate@v1（元行引用扩宽为材料完整证据跨度）。
    """
    method = cfg.get("method") or {}
    kind = str(((method.get("assembly") or {}).get("type")) or "material_template").lower()
    if kind == "material_template":
        return Assembler()
    if kind == "llm":
        if gateway is None or template is None:
            raise ValueError("assembly.type=llm 需要 Assembler 网关与冻结模板")
        spec = method.get("assembly") or {}
        rows = {r.get("evidence_id"): (r.get("text") or "") for r in (evidence_rows or [])
                if isinstance(r, dict) and r.get("evidence_id")}
        from .methods.wheel_release import METHOD, WheelReleaseAssembler
        assembler_class = WheelReleaseAssembler if method.get("name") == METHOD else LLMAssembler
        return assembler_class(
            gateway,
            template,
            max_format_retries=int(spec.get("max_format_retries", 1)),
            max_uncovered_ratio=float(spec.get("max_uncovered_ratio", 0.25)),
            lexical_policy=resolve_lexical_policy(spec.get("lexical_policy")),
            row_text_by_id=rows,
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
