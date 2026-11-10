"""装配与候选选择（手册 §10.1）。

约束：
- 材料来自 StateBackend 提供的 materials（原文行 / 摘要 / 命题），三种后端走同一路径；
- 缺项由要素关联计算（不是"要素名是否出现在答案里"）；
- 每条主张必须带 evidence_ids；缺项保留为缺项；
- 多候选共享同一证据包；选择器只读在线标准。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import AgentState, AnswerCandidate, Claim, Origin, SupportStatus, Task, new_id
from .state import compute_open, element_evidence, normalize_element


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
        for m in state.materials:
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
            lines.append(f"- {key}：{value}" if key else f"- {value}")
            claims.append(
                Claim(
                    claim_id=new_id("cl"),
                    text=value,
                    evidence_ids=list(m.get("evidence_ids") or []),
                    origin=Origin.TARGET_RESPONSE,
                    support_status=SupportStatus.ENTAILED,
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
    "CandidateSelector",
    "CoverageSelector",
    "EvidencePack",
    "make_selector",
    "compute_open",
    "element_evidence",
    "normalize_element",
]
