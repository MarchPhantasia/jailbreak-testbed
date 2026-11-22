"""组件恢复策略（手册 §11）。

原则：
- 先保存原始响应与成本，再校验可用部分；
- 有限重试（网络/格式各至多一次），总上限独立；
- 不编造命题、不伪造完成状态、不修改已发生的成本；
- 未知评分不写零；未知判定不当作成功或失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .contracts import Role, Status
from .events import EventStore


@dataclass
class RecoveryDecision:
    action: str  # retry_same | retry_rephrase | fallback_continue | fallback_assemble | stop
    reason: str
    counted_as: str = "recovery"
    # 归因链（2026-11-22 修复）：本决定事件 ID 与触发它的调用/响应引用
    event_id: str = ""
    trigger_event_id: str = ""
    trigger_call_id: str = ""


class RecoveryPolicy:
    def __init__(
        self,
        store: EventStore,
        *,
        max_network_retries: int = 1,
        max_format_retries: int = 1,
        total_cap: int = 3,
        version: str = "r1",
    ):
        self.store = store
        self.max_network_retries = max_network_retries
        self.max_format_retries = max_format_retries
        self.total_cap = total_cap
        self.version = version
        self._counts: Dict[str, int] = {}
        self._total = 0

    def _bump(self, key: str) -> int:
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def handle(self, role: Role | str, status: Status, *, attempt_key: str = "",
               trigger_event_id: str = "", trigger_call_id: str = "",
               status_error: str = "") -> RecoveryDecision:
        role_name = role.value if isinstance(role, Role) else str(role)
        key = attempt_key or role_name
        if self._total >= self.total_cap:
            decision = RecoveryDecision("stop", "recovery_cap_reached")
        elif status in (Status.TIMEOUT, Status.TRANSPORT_ERROR):
            if self._bump(f"net:{key}") <= self.max_network_retries:
                decision = RecoveryDecision("retry_same", f"network:{status.value}")
            else:
                decision = RecoveryDecision("fallback_continue", "network_retries_exhausted")
        elif status in (Status.INVALID_OUTPUT, Status.INSUFFICIENT_EVIDENCE):
            if self._bump(f"fmt:{key}") <= self.max_format_retries:
                decision = RecoveryDecision("retry_rephrase", f"format:{status.value}")
            else:
                decision = RecoveryDecision("fallback_assemble", "format_retries_exhausted")
        elif status is Status.REFUSED:
            decision = RecoveryDecision("fallback_assemble", "component_refused")
        else:
            decision = RecoveryDecision("fallback_continue", f"no_recovery_needed:{status.value}")

        if decision.action != "fallback_continue" or status is not Status.OK:
            self._total += 1
            ev = self.store.append(
                "recovery_decision",
                role=role_name,
                call_id=trigger_call_id or None,
                payload={
                    "action": decision.action,
                    "reason": decision.reason,
                    "policy_version": self.version,
                    # 归因链：触发本决定的那次调用与响应事件；原始错误原样保留
                    **({"trigger_event_id": trigger_event_id} if trigger_event_id else {}),
                    **({"trigger_call_id": trigger_call_id} if trigger_call_id else {}),
                    **({"status_error": status_error} if status_error else {}),
                },
            )
            decision.event_id = ev.event_id
            decision.trigger_event_id = trigger_event_id
            decision.trigger_call_id = trigger_call_id
            if role_name == Role.CONTROLLER.value and status is Status.REFUSED:
                self.store.append("component_refusal", role=role_name, payload={"reason": "controller_refused"})
        return decision
