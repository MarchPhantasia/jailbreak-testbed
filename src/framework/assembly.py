"""装配与候选选择（手册 §10.1）。

约束：
- 只用证据包里已有的命题组织答案；缺项保留为缺项，不自由补写；
- 每条进入答案的主张必须能映射到 evidence_id，否则标为 hypothesis 并不计入来源；
- 多候选共享同一证据包；记录 k、阶段策略与全部成本；
- CandidateSelector 只能使用在线标准，不得读取 Judge_eval。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import (
    AgentState,
    AnswerCandidate,
    Claim,
    Evidence,
    Origin,
    Proposition,
    SupportStatus,
    Task,
    new_id,
)


@dataclass
class EvidencePack:
    pack_id: str
    task_id: str
    facts: List[Proposition] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def evidence_ids(self) -> List[str]:
        out: List[str] = []
        for f in self.facts:
            out.extend(f.evidence_ids)
        return out


class Assembler:
    """单候选装配是开发起点；k>1 时共享同一证据包。"""

    name = "template"

    def build_pack(self, task: Task, state: AgentState, elements: Optional[List[str]] = None) -> EvidencePack:
        gaps: List[str] = []
        if elements:
            present = " ".join(f.text for f in state.facts)
            gaps = [e for e in elements if e not in present]
        return EvidencePack(pack_id=new_id("pack"), task_id=task.task_id, facts=list(state.facts), gaps=gaps)

    def render(self, task: Task, pack: EvidencePack) -> Tuple[str, List[Claim], List[str]]:
        """确定性模板渲染：按 key 分组，保留原文表述与限定条件。"""
        claims: List[Claim] = []
        lines: List[str] = []
        seen: set[str] = set()
        for fact in pack.facts:
            key = str(fact.qualifiers.get("key", "") or "")
            if fact.text in seen:
                continue
            seen.add(fact.text)
            prefix = f"{key}：" if key else ""
            lines.append(f"- {prefix}{fact.text}")
            claims.append(
                Claim(
                    claim_id=new_id("cl"),
                    text=fact.text,
                    evidence_ids=list(fact.evidence_ids),
                    origin=fact.origin,
                    support_status=fact.support_status,
                )
            )
        if pack.gaps:
            lines.append("- 未提供：" + "、".join(pack.gaps))
        body = "\n".join(lines) if lines else "（无可装配材料）"
        # 不回显原始请求：否则"要素名出现在 prompt 里"会被判据误当成覆盖
        answer = body
        return answer, claims, list(pack.gaps)

    def assemble(
        self,
        task: Task,
        state: AgentState,
        *,
        k: int = 1,
        elements: Optional[List[str]] = None,
        stage_policy_ref: str = "",
    ) -> List[AnswerCandidate]:
        pack = self.build_pack(task, state, elements)
        candidates: List[AnswerCandidate] = []
        for i in range(max(1, k)):
            answer, claims, gaps = self.render(task, pack)
            if i > 0:
                # 追加式变体：仅改变组织顺序（不新增内容），用于候选池实验
                answer = answer + f"\n（排版变体 {i}）"
            candidates.append(
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
        return candidates


class CandidateSelector:
    """在线选择：按预先声明的标准，不看最终评价。"""

    name = "first"

    def select(self, candidates: List[AnswerCandidate], scores: Optional[Dict[str, float]] = None) -> Optional[AnswerCandidate]:
        if not candidates:
            return None
        if scores:
            return max(candidates, key=lambda c: scores.get(c.candidate_id, -1.0))
        return candidates[0]


class CoverageSelector(CandidateSelector):
    """按"有证据的主张数 - 缺项数"选择；仍是确定性在线标准。"""

    name = "coverage"

    def select(self, candidates: List[AnswerCandidate], scores: Optional[Dict[str, float]] = None):
        if not candidates:
            return None

        def key(c: AnswerCandidate) -> int:
            supported = sum(1 for cl in c.claims if cl.evidence_ids)
            return supported - len(c.gaps)

        return max(candidates, key=key)


def make_selector(name: str) -> CandidateSelector:
    name = (name or "first").lower()
    if name == "coverage":
        return CoverageSelector()
    return CandidateSelector()
