"""核心数据契约（手册 §5）。

设计原则：
- 只放数据对象与枚举，不放行为；
- 所有字段可序列化（dataclasses.asdict），便于写入事件与清单；
- 状态枚举区分"接口状态"（OK/PARTIAL/...）与"语义状态"（INSUFFICIENT_EVIDENCE）；
- 未知一律显式表达（unknown / not_assessed / unscored），不用零或空串代替。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
class Status(str, Enum):
    """接口状态（手册 §4）。语义任务另用 INSUFFICIENT_EVIDENCE。"""

    OK = "OK"
    PARTIAL = "PARTIAL"
    REFUSED = "REFUSED"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    TRUNCATED = "TRUNCATED"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ActionType(str, Enum):
    ASK_TARGET = "ASK_TARGET"
    ASSEMBLE = "ASSEMBLE"
    STOP = "STOP"


class Origin(str, Enum):
    TARGET_RESPONSE = "target_response"
    INPUT = "input"
    CONTROLLER_HYPOTHESIS = "controller_hypothesis"
    DERIVED = "derived"


class Fidelity(str, Enum):
    """形成方式（不是正确性）。"""

    VERBATIM = "verbatim"
    PARAPHRASE = "paraphrase"
    INFERRED = "inferred"


class FidelityStatus(str, Enum):
    PRESERVED = "preserved"
    ALTERED = "altered"
    UNCERTAIN = "uncertain"
    NOT_ASSESSED = "not_assessed"
    NOT_APPLICABLE = "not_applicable"


class SupportStatus(str, Enum):
    ENTAILED = "entailed"
    CONTRADICTED = "contradicted"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


class TruthStatus(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNKNOWN = "unknown"
    NOT_ASSESSED = "not_assessed"


class SessionMode(str, Enum):
    STATELESS = "stateless"
    SERVER_SIDE = "server_side"


class TargetLabel(str, Enum):
    """单轮 Target 行为标签（以当前子请求为参照）。"""

    REFUSAL = "refusal"
    SOFT_REFUSAL = "soft_refusal"
    PARTIAL_COMPLIANCE = "partial_compliance"
    FULL_COMPLIANCE = "full_compliance"
    UNKNOWN = "unknown"


class EpisodeOutcome(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    UNRESOLVED = "unresolved"  # 对应指标里的 U


class Phase(str, Enum):
    ONLINE = "online"
    ANALYSIS = "analysis"
    PREFLIGHT = "preflight"


class Role(str, Enum):
    CONTROLLER = "controller"
    TARGET = "target"
    EXTRACTOR = "extractor"
    SCORER = "scorer"
    ASSEMBLER = "assembler"
    SELECTOR = "selector"
    JUDGE_ONLINE = "judge_online"
    JUDGE_EVAL = "judge_eval"
    SUMMARIZER = "summarizer"


class NodeStatus(str, Enum):
    OPEN = "open"
    SELECTED = "selected"
    CLOSED = "closed"


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def stable_hash(obj: Any) -> str:
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 数据契约
# --------------------------------------------------------------------------
@dataclass
class Task:
    """手册 §5.1。评价侧材料（rubric/reference）只允许评价流程读取。"""

    task_id: str
    prompt: str
    primary_dataset: str
    necessary_context: str = ""
    category: str = ""
    upstream_id: str = ""
    source_aliases: List[str] = field(default_factory=list)
    source_locator: str = ""
    dataset_revision: str = ""
    profile: str = ""
    split: str = ""
    duplicate_group_id: str = ""
    eval_materials: Dict[str, Any] = field(default_factory=dict)  # 仅评价侧可读
    runtime_hints: Dict[str, Any] = field(default_factory=dict)  # 仅 fixtures：模拟 Target 的脚本，不进入 Controller
    source_license: str = ""

    def public_view(self) -> Dict[str, Any]:
        """Controller 可见的字段（不含 eval_materials）。"""
        return {
            "task_id": self.task_id,
            "prompt": self.prompt,
            "necessary_context": self.necessary_context,
            "category": self.category,
        }


@dataclass
class BudgetLimits:
    """预算上限。主比较固定 target_calls；其余维度用于记账。

    stage_calls 是"在线阶段调用上限"（E01-R2）：与总额同时检查，任一不足都不得发出。
    阶段限额不能创造新总额，只会在总额之内进一步收窄。
    """

    target_calls: int = 20
    non_target_calls: int = 400
    tokens: Optional[int] = None
    wall_ms: Optional[int] = None
    stage_calls: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Episode:
    """手册 §5.1。一次完整越狱尝试的元信息。"""

    episode_id: str
    task_id: str
    method_version: str
    config_hash: str
    budget: BudgetLimits
    session_mode: SessionMode = SessionMode.STATELESS
    dataset_manifest_hash: str = ""
    split_manifest_hash: str = ""
    model_records: Dict[str, Any] = field(default_factory=dict)
    stage_policy_ref: str = ""
    verification_policy_ref: str = ""
    recovery_policy_version: str = "r1"
    seed: int = 0
    deployment_manifest_hash: str = ""


@dataclass
class Evidence:
    """手册 §5.3。原文片段与定位；范围用 Unicode code point 半开区间。"""

    evidence_id: str
    source_event_id: str
    role: str
    text: str
    span_start: int
    span_end: int
    checksum: str

    @staticmethod
    def from_text(event_id: str, role: str, text: str, start: int, end: int) -> "Evidence":
        span = text[start:end]
        return Evidence(
            evidence_id=new_id("ev"),
            source_event_id=event_id,
            role=role,
            text=span,
            span_start=start,
            span_end=end,
            checksum=hashlib.sha256(span.encode("utf-8")).hexdigest(),
        )


@dataclass
class Proposition:
    """手册 §5.3 的命题（事实）。"""

    fact_id: str
    text: str
    origin: Origin
    evidence_ids: List[str] = field(default_factory=list)
    fidelity: Fidelity = Fidelity.VERBATIM
    support_status: SupportStatus = SupportStatus.UNKNOWN
    qualifiers: Dict[str, Any] = field(default_factory=dict)
    dependency_fact_ids: List[str] = field(default_factory=list)
    processing_refs: List[str] = field(default_factory=list)
    extractor_ref: str = ""
    fidelity_status: FidelityStatus = FidelityStatus.NOT_ASSESSED
    truth_status: TruthStatus = TruthStatus.NOT_ASSESSED


@dataclass
class AgentState:
    """手册 §5.2。facts 是"按证据规则保存的命题"，不等于已验证真实。"""

    facts: List[Proposition] = field(default_factory=list)
    summary: str = ""  # 摘要后端的当前摘要；原文后端为空串
    materials: List[Dict[str, Any]] = field(default_factory=list)  # 各后端可被装配消费的材料（带 evidence_ids）
    open: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    budget_left: Dict[str, Any] = field(default_factory=dict)
    source_event_seq: int = 0
    node_id: str = ""
    parent_state_seq: int = 0
    extractor_version: str = ""

    def fact_texts(self) -> List[str]:
        return [f.text for f in self.facts]


@dataclass
class HistoryView:
    """手册 §5.2 / §7.3。view 只控制客户端提交内容；服务端是否遗忘另记。"""

    view_id: str
    parent_view_id: Optional[str]
    visible_message_refs: List[str] = field(default_factory=list)
    session_mode: SessionMode = SessionMode.STATELESS


@dataclass
class Action:
    action_id: str
    type: ActionType
    params: Dict[str, Any] = field(default_factory=dict)
    based_on: List[str] = field(default_factory=list)
    based_on_state_seq: int = 0
    node_id: Optional[str] = None
    view_id: Optional[str] = None
    rationale: str = ""


@dataclass
class SearchNode:
    node_id: str
    parent_node_id: Optional[str]
    state_snapshot_ref: str
    view_ref: str
    action_ref: Optional[str] = None
    request_ref: Optional[str] = None
    response_ref: Optional[str] = None
    score_ref: Optional[str] = None
    cost: Dict[str, Any] = field(default_factory=dict)
    status: NodeStatus = NodeStatus.OPEN


@dataclass
class Claim:
    """答案中的一条主张与证据的映射。"""

    claim_id: str
    text: str
    evidence_ids: List[str] = field(default_factory=list)
    origin: Origin = Origin.TARGET_RESPONSE
    support_status: SupportStatus = SupportStatus.UNKNOWN
    fidelity: Fidelity = Fidelity.VERBATIM


@dataclass
class AnswerCandidate:
    candidate_id: str
    answer: str
    claims: List[Claim] = field(default_factory=list)
    evidence_pack_ids: List[str] = field(default_factory=list)
    derivation_refs: List[str] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    assembly_config: Dict[str, Any] = field(default_factory=dict)
    stage_policy_ref: str = ""
    processing_refs: List[str] = field(default_factory=list)
    fallback_render: bool = False
    end_state: str = ""


@dataclass
class Verdict:
    """最终评价（Judge_eval）。unscored 属于"评价可用性"，不是内容状态。"""

    verdict_id: str
    eval_run_id: str
    task_id: str
    answer_id: Optional[str]
    rubric_version: str
    judge_ref: str
    payload: Dict[str, Any] = field(default_factory=dict)
    cls: Optional[str] = None  # A/B/C/D；不可用为 None
    coverage: Optional[float] = None
    usable: bool = True
    basis: str = ""


@dataclass
class StagePolicy:
    """手册 §5.5 / §8.1。阶段映射与继承；不预设具体档位。"""

    common: Dict[str, Any] = field(default_factory=dict)
    stages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    inheritance: Dict[str, str] = field(default_factory=dict)
    service_capabilities: Dict[str, Any] = field(default_factory=dict)

    def resolve(self, stage_id: str) -> Dict[str, Any]:
        """按继承链合并配置；未覆盖项继承 common。"""
        chain: List[str] = []
        cur = stage_id
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.inheritance.get(cur, "")
        resolved: Dict[str, Any] = dict(self.common)
        for node in reversed(chain):
            resolved.update(self.stages.get(node, {}))
        return resolved


@dataclass
class VerificationPolicy:
    enabled: bool = False
    targets: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    budget_owner: str = "online"
    order: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetManifest:
    source_url: str
    revision: str
    raw_sha256: str
    adapter_version: str
    filter_version: str
    split_seed: int
    tasks: List[str] = field(default_factory=list)
    duplicate_groups: Dict[str, List[str]] = field(default_factory=dict)
    included: int = 0
    excluded: List[Dict[str, str]] = field(default_factory=list)
    source_license: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
