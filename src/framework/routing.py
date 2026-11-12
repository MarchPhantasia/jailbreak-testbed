"""服务与组件路由、模型参数档案、冻结清单（E01-R2）。

外部契约见 contracts/MODEL_ROUTING_CONTRACT.md 与 MODEL_ROUTING.md：

- ``services`` 是服务 ID → 客户端配置；``components`` 是角色 ID → enabled/service_ref/stage_id；
- 组件到服务、服务到模型是**配置关系**，代码里不按角色名写死；
- 旧 ``target`` / ``attacker`` / ``evaluation.service`` 仍兼容，分别归一化为
  target、controller+assembler、judge_eval；新旧字段混用直接报错，不猜优先级；
- profile 只定义"序列化/校验规则"，不证明后端已经支持；
- 所有参数校验发生在任何 preflight 生成调用之前。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import BudgetLimits

#: 三个初始参数档案（R2）
PROFILE_OPENAI = "openai_chat"
PROFILE_QWEN = "qwen38_vllm"
PROFILE_DEEPSEEK = "deepseek_thinking_proxy"
PROFILES = (PROFILE_OPENAI, PROFILE_QWEN, PROFILE_DEEPSEEK)

#: 模型原生思考档位：不能互相换算，也不能塞入非原生值
NATIVE_EFFORTS = {
    PROFILE_QWEN: ("low", "medium", "xhigh"),
    PROFILE_DEEPSEEK: ("low", "high", "max"),
}

#: 保留键：任何情况下都不得由扩展字段覆盖
RESERVED_BODY_KEYS = ("model", "messages", "stream", "tools", "tool_choice", "n")

ROLE_ORDER = ("target", "controller", "assembler", "judge_eval", "extractor", "summarizer", "judge_online")
#: 旧配置的对外键名（N02 要求 target/attacker/judge_eval 三个键）
LEGACY_HEALTH_KEYS = ("target", "attacker", "judge_eval")


class RoutingError(ValueError):
    """路由或参数配置非法；不得回退为 mock 或默认参数。"""


# --------------------------------------------------------------------------
# 数值校验：布尔、NaN、越界都显式拒绝
# --------------------------------------------------------------------------
def _number(name: str, value: Any, *, lo: float, hi: float, integer: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingError(f"参数 {name} 必须是{'整数' if integer else '数值'}，收到 {value!r}")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise RoutingError(f"参数 {name} 不能是 NaN/Inf")
    if integer:
        if int(value) != value:
            raise RoutingError(f"参数 {name} 必须是整数，收到 {value!r}")
        value = int(value)
    if not (lo <= value <= hi):
        raise RoutingError(f"参数 {name} 超出范围 [{lo}, {hi}]：{value}")
    return value


def _positive_int(name: str, value: Any) -> int:
    v = _number(name, value, lo=1, hi=10**9, integer=True)
    return int(v)


# --------------------------------------------------------------------------
# profile 序列化 / 校验
# --------------------------------------------------------------------------
def serialize_openai_chat(params: Dict[str, Any]) -> Dict[str, Any]:
    """标准 chat completions 字段；本轮未声明的扩展一律拒绝。"""
    allowed = {"temperature", "top_p", "max_tokens", "stop", "seed"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise RoutingError(f"profile=openai_chat 不支持参数：{unknown}")
    out: Dict[str, Any] = {}
    if "temperature" in params:
        out["temperature"] = _number("temperature", params["temperature"], lo=0.0, hi=2.0)
    if "top_p" in params:
        out["top_p"] = _number("top_p", params["top_p"], lo=0.0, hi=1.0)
        if out["top_p"] <= 0:
            raise RoutingError("参数 top_p 必须大于 0")
    if "max_tokens" in params:
        out["max_tokens"] = _positive_int("max_tokens", params["max_tokens"])
    if "stop" in params:
        out["stop"] = params["stop"]
    if "seed" in params:
        out["seed"] = _positive_int("seed", params["seed"])
    return out


def serialize_qwen38_vllm(params: Dict[str, Any]) -> Dict[str, Any]:
    """Qwen/vLLM：top_k 与 chat_template_kwargs 进顶层；effort 与模板参数一致。"""
    allowed_top = {"temperature", "top_p", "top_k", "max_tokens", "stop", "seed", "reasoning_effort"}
    allowed_extra = {"top_k", "chat_template_kwargs"}
    allowed_template = {"enable_thinking", "preserve_thinking", "reasoning_effort"}

    body = dict(params)
    extra = body.pop("extra_body", None)
    if extra is not None and not isinstance(extra, dict):
        raise RoutingError("extra_body 必须是对象")
    template_kwargs: Dict[str, Any] = {}
    for key, value in (extra or {}).items():
        if key in RESERVED_BODY_KEYS:
            raise RoutingError(f"extra_body 不得覆盖保留键：{key}")
        if key == "chat_template_kwargs":
            if not isinstance(value, dict):
                raise RoutingError("chat_template_kwargs 必须是对象")
            template_kwargs = dict(value)
            continue
        if key not in allowed_extra:
            raise RoutingError(f"profile=qwen38_vllm 不接受扩展字段：{key}")
        if key in body and body[key] != value:
            raise RoutingError(f"参数 {key} 在顶层与 extra_body 中取值冲突")
        body[key] = value

    unknown = sorted(set(body) - allowed_top)
    if unknown:
        raise RoutingError(f"profile=qwen38_vllm 不支持参数：{unknown}")
    unknown_tpl = sorted(set(template_kwargs) - allowed_template)
    if unknown_tpl:
        raise RoutingError(f"chat_template_kwargs 不支持键：{unknown_tpl}")

    out: Dict[str, Any] = {}
    if "temperature" in body:
        out["temperature"] = _number("temperature", body["temperature"], lo=0.0, hi=2.0)
    if "top_p" in body:
        out["top_p"] = _number("top_p", body["top_p"], lo=0.0, hi=1.0)
        if out["top_p"] <= 0:
            raise RoutingError("参数 top_p 必须大于 0")
    if "top_k" in body:
        out["top_k"] = _positive_int("top_k", body["top_k"])
    if "max_tokens" in body:
        out["max_tokens"] = _positive_int("max_tokens", body["max_tokens"])
    if "stop" in body:
        out["stop"] = body["stop"]
    if "seed" in body:
        out["seed"] = _positive_int("seed", body["seed"])

    enable = template_kwargs.get("enable_thinking", True)
    if not isinstance(enable, bool):
        raise RoutingError("chat_template_kwargs.enable_thinking 必须是布尔值")
    top_effort = body.get("reasoning_effort")
    tpl_effort = template_kwargs.get("reasoning_effort")
    effort = top_effort if top_effort is not None else tpl_effort
    if top_effort is not None and tpl_effort is not None and top_effort != tpl_effort:
        raise RoutingError("reasoning_effort 在顶层与 chat_template_kwargs 中不一致")
    if effort is not None:
        if effort not in NATIVE_EFFORTS[PROFILE_QWEN]:
            raise RoutingError(
                f"profile=qwen38_vllm 的 reasoning_effort 只接受 {NATIVE_EFFORTS[PROFILE_QWEN]}：{effort!r}"
            )
        if not enable:
            raise RoutingError("thinking 关闭时不能配置 reasoning_effort（不会被发送）")
        out["reasoning_effort"] = effort

    tpl_out: Dict[str, Any] = {"enable_thinking": enable}
    if "preserve_thinking" in template_kwargs:
        if not isinstance(template_kwargs["preserve_thinking"], bool):
            raise RoutingError("chat_template_kwargs.preserve_thinking 必须是布尔值")
        tpl_out["preserve_thinking"] = template_kwargs["preserve_thinking"]
    else:
        tpl_out["preserve_thinking"] = False
    if effort is not None:
        tpl_out["reasoning_effort"] = effort
    out["chat_template_kwargs"] = tpl_out
    return out


def serialize_deepseek_thinking_proxy(params: Dict[str, Any]) -> Dict[str, Any]:
    """DeepSeek thinking：不发送 temperature/top_p，effort 只接受原生 low/high/max。"""
    allowed_top = {"max_tokens", "reasoning_effort"}
    body = dict(params)
    extra = body.pop("extra_body", None)
    if extra is not None and not isinstance(extra, dict):
        raise RoutingError("extra_body 必须是对象")
    thinking: Dict[str, Any] = {"type": "enabled"}
    for key, value in (extra or {}).items():
        if key in RESERVED_BODY_KEYS:
            raise RoutingError(f"extra_body 不得覆盖保留键：{key}")
        if key != "thinking":
            raise RoutingError(f"profile=deepseek_thinking_proxy 不接受扩展字段：{key}")
        if not isinstance(value, dict):
            raise RoutingError("thinking 必须是对象")
        thinking = dict(value)

    for banned in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "logit_bias", "seed", "stop"):
        if banned in body:
            raise RoutingError(f"thinking 模式下参数 {banned} 无效，不能配置后宣称生效")
    unknown = sorted(set(body) - allowed_top)
    if unknown:
        raise RoutingError(f"profile=deepseek_thinking_proxy 不支持参数：{unknown}")
    if thinking.get("type") != "enabled":
        raise RoutingError("本轮 deepseek_thinking_proxy 只支持 thinking.type=enabled")

    out: Dict[str, Any] = {}
    if "max_tokens" in body:
        out["max_tokens"] = _positive_int("max_tokens", body["max_tokens"])
    effort = body.get("reasoning_effort")
    if effort is not None:
        if effort not in NATIVE_EFFORTS[PROFILE_DEEPSEEK]:
            raise RoutingError(
                f"profile=deepseek_thinking_proxy 的 reasoning_effort 只接受 "
                f"{NATIVE_EFFORTS[PROFILE_DEEPSEEK]}（medium 与 high 不是同一档）：{effort!r}"
            )
        out["reasoning_effort"] = effort
    out["thinking"] = thinking
    return out


SERIALIZERS = {
    PROFILE_OPENAI: serialize_openai_chat,
    PROFILE_QWEN: serialize_qwen38_vllm,
    PROFILE_DEEPSEEK: serialize_deepseek_thinking_proxy,
}


def serialize_params(profile: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if profile not in SERIALIZERS:
        raise RoutingError(f"未知 parameter_profile：{profile}（可用：{list(PROFILES)}）")
    return SERIALIZERS[profile](dict(params or {}))


# --------------------------------------------------------------------------
# 路由数据
# --------------------------------------------------------------------------
@dataclass
class ServiceConfig:
    service_id: str
    type: str
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    auth_required: bool = False
    timeout_s: float = 60.0
    max_concurrency: int = 1
    parameter_profile: str = PROFILE_OPENAI

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_id": self.service_id,
            "type": self.type,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "auth_required": self.auth_required,
            "timeout_s": self.timeout_s,
            "max_concurrency": self.max_concurrency,
            "parameter_profile": self.parameter_profile,
        }


@dataclass
class ComponentRoute:
    role: str
    enabled: bool
    service_ref: str
    stage_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "enabled": self.enabled, "service_ref": self.service_ref, "stage_id": self.stage_id}


@dataclass
class RoutingPlan:
    services: Dict[str, ServiceConfig]
    components: Dict[str, ComponentRoute]
    stages_common: Dict[str, Any] = field(default_factory=dict)
    stages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    inheritance: Dict[str, str] = field(default_factory=dict)
    new_registry: bool = False
    stage_ref: str = ""
    _resolved: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ---- 访问 ----
    def route(self, role: str) -> Optional[ComponentRoute]:
        return self.components.get(role)

    def enabled_roles(self) -> List[str]:
        return [r for r in ROLE_ORDER if r in self.components and self.components[r].enabled]

    def service(self, route: ComponentRoute) -> ServiceConfig:
        svc = self.services.get(route.service_ref)
        if svc is None:
            raise RoutingError(f"角色 {route.role} 引用了不存在的服务：{route.service_ref}")
        return svc

    def raw_stage_params(self, stage_id: str) -> Dict[str, Any]:
        resolved: Dict[str, Any] = dict(self.stages_common)
        chain: List[str] = []
        cur = stage_id
        seen: set = set()
        while cur and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            cur = self.inheritance.get(cur, "")
        for node in reversed(chain):
            resolved.update(self.stages.get(node, {}) or {})
        return resolved

    def resolved_params(self, role: str) -> Dict[str, Any]:
        """角色的最终发送参数（扩展已展开）；未启用角色空字典。"""
        if role in self._resolved:
            return dict(self._resolved[role])
        route = self.components.get(role)
        if route is None or not route.enabled:
            return {}
        svc = self.service(route)
        raw = self.raw_stage_params(route.stage_id)
        if svc.type == "mock":
            self._resolved[role] = {}
            return {}
        resolved = serialize_params(svc.parameter_profile, raw)
        self._resolved[role] = resolved
        return dict(resolved)

    def serializer_for(self, role: str):
        svc = self.service(self.components[role])
        profile = svc.parameter_profile
        return lambda params: serialize_params(profile, params)

    # ---- 清单 ----
    def manifest(self, capability: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        capability = capability or {}
        for role in self.enabled_roles():
            route = self.components[role]
            svc = self.service(route)
            row = {
                "role": role,
                "stage_id": route.stage_id,
                "service_id": svc.service_id,
                "type": svc.type,
                "base_url": svc.base_url,
                "model": svc.model,
                "parameter_profile": svc.parameter_profile,
                "api_key_env": svc.api_key_env,
                "auth_required": svc.auth_required,
                "timeout_s": svc.timeout_s,
                "max_concurrency": svc.max_concurrency,
                "resolved_params": self.resolved_params(role),
                "capability_status": capability.get(role)
                or {
                    "documented": "profile_spec_only",
                    "transport_accepted": "unknown",
                    "backend_confirmed": "unknown",
                    "status": "unknown",
                },
                "model_revision": "unknown",
                "server_version": "unknown",
                "template_hash": "unknown",
            }
            rows.append(row)
        return rows

    def api_key_of(self, role: str) -> str:
        svc = self.service(self.components[role])
        return svc.api_key_env


# --------------------------------------------------------------------------
# 从配置构建
# --------------------------------------------------------------------------
def _service_from_legacy(service_id: str, raw: Dict[str, Any], profile: str) -> ServiceConfig:
    kind = str(raw.get("type") or "mock").lower()
    if kind not in ("mock", "openai", "openai_compatible", "http"):
        raise RoutingError(f"未支持的服务类型：{kind}（不做隐式 mock 回退）")
    if kind != "mock" and not raw.get("base_url"):
        raise RoutingError(f"服务 {service_id} 缺少 base_url")
    if kind != "mock" and not raw.get("model"):
        raise RoutingError(f"服务 {service_id} 缺少 model")
    return ServiceConfig(
        service_id=service_id,
        type="mock" if kind == "mock" else "openai",
        base_url=str(raw.get("base_url") or ""),
        model=str(raw.get("model") or ""),
        api_key_env=str(raw.get("api_key_env") or ""),
        auth_required=bool(raw.get("auth_required", False)),
        timeout_s=float(raw.get("timeout_s", 60)),
        max_concurrency=int(raw.get("max_concurrency", 1)),
        parameter_profile=profile,
    )


def _validate_stages(cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], Dict[str, str], str]:
    stages_cfg = cfg.get("stages") or {}
    if not isinstance(stages_cfg, dict):
        raise RoutingError("stages 必须是对象")
    common = dict(stages_cfg.get("common") or {})
    stages = dict(stages_cfg.get("stages") or {})
    inheritance = dict(stages_cfg.get("inheritance") or {})
    for stage, spec in stages.items():
        if not isinstance(spec, dict):
            raise RoutingError(f"stages.stages.{stage} 必须是对象")
    return common, stages, inheritance, str(stages_cfg.get("ref") or "")


def resolve_routing(cfg: Dict[str, Any]) -> RoutingPlan:
    """构建路由并完成全部参数校验；任何非法配置在此抛错（任何调用之前）。"""
    method = cfg.get("method") or {}
    assembly_type = str(((method.get("assembly") or {}).get("type")) or "material_template").lower()
    controller_type = str(((method.get("controller") or {}).get("type")) or "element").lower()
    evaluation = cfg.get("evaluation") or {}
    eval_type = str(evaluation.get("type") or "rule").lower()

    common, stages, inheritance, stage_ref = _validate_stages(cfg)
    has_new = ("services" in cfg) or ("components" in cfg)
    legacy_fields = [k for k in ("target", "attacker") if k in cfg] + (
        ["evaluation.service"] if "service" in evaluation else []
    )

    services: Dict[str, ServiceConfig] = {}
    components: Dict[str, ComponentRoute] = {}

    if has_new:
        if legacy_fields:
            raise RoutingError(
                f"配置同时包含新 services/components 与旧服务字段 {legacy_fields}；"
                "两种入口只能选一种，不猜优先级"
            )
        raw_services = cfg.get("services") or {}
        if not isinstance(raw_services, dict) or not raw_services:
            raise RoutingError("services 必须是非空映射")
        for sid, raw in raw_services.items():
            if not isinstance(raw, dict):
                raise RoutingError(f"services.{sid} 必须是对象")
            kind = str(raw.get("type") or "openai").lower()
            if kind not in ("openai", "openai_compatible", "http", "mock"):
                raise RoutingError(f"服务 {sid} 的类型未支持：{kind}")
            profile = str(raw.get("parameter_profile") or "")
            if not profile:
                raise RoutingError(f"服务 {sid} 缺少 parameter_profile")
            if profile not in PROFILES:
                raise RoutingError(f"服务 {sid} 的 parameter_profile 未知：{profile}")
            if kind != "mock":
                if not raw.get("base_url"):
                    raise RoutingError(f"服务 {sid} 缺少 base_url")
                if not raw.get("model"):
                    raise RoutingError(f"服务 {sid} 缺少 model")
            services[sid] = ServiceConfig(
                service_id=sid,
                type="mock" if kind == "mock" else "openai",
                base_url=str(raw.get("base_url") or ""),
                model=str(raw.get("model") or ""),
                api_key_env=str(raw.get("api_key_env") or ""),
                auth_required=bool(raw.get("auth_required", True)),
                timeout_s=float(raw.get("timeout_s", 60)),
                max_concurrency=int(raw.get("max_concurrency", 1)),
                parameter_profile=profile,
            )
        raw_components = cfg.get("components") or {}
        if not isinstance(raw_components, dict):
            raise RoutingError("components 必须是映射")
        for role, raw in raw_components.items():
            if role not in ROLE_ORDER:
                raise RoutingError(f"未知组件角色：{role}（可用：{list(ROLE_ORDER)}）")
            if not isinstance(raw, dict):
                raise RoutingError(f"components.{role} 必须是对象")
            enabled = bool(raw.get("enabled", True))
            service_ref = str(raw.get("service_ref") or "")
            stage_id = str(raw.get("stage_id") or "")
            if enabled:
                if service_ref not in services:
                    raise RoutingError(f"角色 {role} 引用了不存在的服务：{service_ref or '(空)'}")
                if not stage_id:
                    raise RoutingError(f"角色 {role} 缺少 stage_id")
                if stage_id not in stages and stage_id not in inheritance:
                    raise RoutingError(f"角色 {role} 的 stage_id 未在 stages 中定义：{stage_id}")
            components[role] = ComponentRoute(role=role, enabled=enabled, service_ref=service_ref, stage_id=stage_id)
    else:
        # ---- 旧入口兼容：target / attacker / evaluation.service ----
        target = cfg.get("target") or {"type": "mock"}
        services["target"] = _service_from_legacy("target", target, PROFILE_OPENAI)
        components["target"] = ComponentRoute("target", True, "target", "target_query")
        if "attacker" in cfg:
            services["attacker"] = _service_from_legacy("attacker", cfg.get("attacker") or {"type": "mock"}, PROFILE_OPENAI)
            components["controller"] = ComponentRoute("controller", True, "attacker", "plan")
            components["assembler"] = ComponentRoute("assembler", assembly_type == "llm", "attacker", "assemble")
        if eval_type == "llm":
            service = evaluation.get("service")
            if not isinstance(service, dict):
                raise RoutingError("evaluation.type=llm 需要 evaluation.service（或改用 components.judge_eval）")
            services["judge"] = _service_from_legacy("judge", service, PROFILE_OPENAI)
            components["judge_eval"] = ComponentRoute(
                "judge_eval", True, "judge", str(evaluation.get("stage_id") or "judge_eval")
            )

    plan = RoutingPlan(
        services=services,
        components=components,
        stages_common=common,
        stages=stages,
        inheritance=inheritance,
        new_registry=has_new,
        stage_ref=stage_ref,
    )

    plan._resolved = {}
    if has_new:
        # 新注册表：启用角色必须是显式配置，缺失绝不退回 mock
        if controller_type == "llm":
            _require_enabled(plan, "controller", "method.controller.type=llm")
        if assembly_type == "llm":
            _require_enabled(plan, "assembler", "method.assembly.type=llm")
        if eval_type == "llm":
            _require_enabled(plan, "judge_eval", "evaluation.type=llm")
    else:
        # 旧入口：保留历史默认（测试用 mock 注入），但装配与评价这两个新功能不许隐式降级
        if assembly_type == "llm":
            _require_enabled(plan, "assembler", "method.assembly.type=llm")
    _require_enabled(plan, "target", "episode 需要 Target")

    # 所有启用角色：先解析并校验参数（在任何 preflight 模型调用之前）
    for role in plan.enabled_roles():
        svc = plan.service(plan.components[role])
        if svc.type == "mock":
            continue
        if not plan.components[role].stage_id:
            raise RoutingError(f"角色 {role} 缺少 stage_id")
        plan.resolved_params(role)
    return plan


def _require_enabled(plan: RoutingPlan, role: str, why: str) -> None:
    route = plan.components.get(role)
    if route is None or not route.enabled:
        raise RoutingError(f"{why} 需要启用组件 {role}（当前缺失或 disabled）；不回退为 mock")


def validate_budget(cfg: Dict[str, Any]) -> BudgetLimits:
    """在线预算：全局 target/non_target + 阶段限额；未实现的硬限额必须显式拒绝。"""
    budget = ((cfg.get("experiment") or {}).get("budget")) or {}
    for key in ("tokens", "wall_ms"):
        if budget.get(key) is not None:
            raise RoutingError(
                f"experiment.budget.{key}={budget.get(key)}：该维度尚无硬约束实现，"
                "不能用事后统计冒充额度；请设为 null 或先实现强制中断"
            )
    stage_calls = budget.get("stage_calls") or {}
    if not isinstance(stage_calls, dict):
        raise RoutingError("experiment.budget.stage_calls 必须是 stage → 上限 的映射")
    parsed_stage: Dict[str, int] = {}
    for stage, cap in stage_calls.items():
        if cap is None:
            continue
        parsed_stage[str(stage)] = _positive_int(f"stage_calls.{stage}", cap)
    return BudgetLimits(
        target_calls=int(budget.get("target_calls", 20)),
        non_target_calls=int(budget.get("non_target_calls", 400)),
        tokens=None,
        wall_ms=None,
        stage_calls=parsed_stage,
    )


def validate_evaluation(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """语义评价契约必须在启动前完整；空契约不能产出"覆盖 100% 的 0% ASR"。"""
    evaluation = cfg.get("evaluation") or {}
    eval_type = str(evaluation.get("type") or "rule").lower()
    if eval_type not in ("llm", "rule"):
        raise RoutingError(f"evaluation.type 必须是 llm 或 rule：{eval_type}")
    if eval_type == "rule":
        return {"type": "rule", "judge_ref": "rule@v1", "rubric": remove_none(evaluation.get("rubric") or {})}
    rubric = evaluation.get("rubric") or {}
    if not isinstance(rubric, dict) or not rubric:
        raise RoutingError("evaluation.type=llm 需要非空语义评价契约（rubric.objective/success_rule）")
    objective = str(rubric.get("objective") or "").strip()
    success_rule = rubric.get("success_rule")
    if not objective:
        raise RoutingError("语义评价契约缺少 objective")
    if not isinstance(success_rule, dict) or not success_rule:
        raise RoutingError("语义评价契约缺少 success_rule")
    allowed = {
        "task_fulfillment": {"complete", "partial", "none", "uncertain"},
        "source_compliance": {"met", "not_met", "unknown"},
        "evidence_sufficiency": {"sufficient", "insufficient", "unknown"},
    }
    rule: Dict[str, str] = {}
    for key, raw in success_rule.items():
        if key not in allowed:
            raise RoutingError(f"success_rule 含未知维度：{key}")
        if raw not in allowed[key]:
            raise RoutingError(f"success_rule.{key} 取值非法：{raw!r}（可用：{sorted(allowed[key])}）")
        rule[key] = str(raw)
    if not rule:
        raise RoutingError("success_rule 为空")
    budget = (evaluation.get("budget") or {}).get("non_target_calls")
    return {
        "type": "llm",
        "rubric": {
            "id": str(rubric.get("id") or "semantic"),
            "version": str(rubric.get("version") or "v1"),
            "objective": objective,
            "success_rule": rule,
        },
        "stage_id": str(evaluation.get("stage_id") or "judge_eval"),
        "max_format_retries": int(evaluation.get("max_format_retries", 1)),
        "budget": {"non_target_calls": int(budget) if budget is not None else 100},
    }


def remove_none(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: remove_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [remove_none(v) for v in obj]
    return obj


def probe_params(profile: str, preflight: Dict[str, Any]) -> Dict[str, Any]:
    """探针参数：按 profile 取轻量档位；1024 是硬生成上限。"""
    profiles = preflight.get("profiles") or {}
    spec = profiles.get(profile)
    if isinstance(spec, dict):
        raw = dict(spec)
    elif profile == PROFILE_QWEN:
        raw = {
            "max_tokens": 1024,
            "reasoning_effort": "low",
            "extra_body": {"top_k": 20, "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": False}},
        }
    elif profile == PROFILE_DEEPSEEK:
        raw = {"max_tokens": 1024, "reasoning_effort": "low", "extra_body": {"thinking": {"type": "enabled"}}}
    else:
        raw = {"temperature": 0.0, "max_tokens": 128}
    if "max_tokens" in raw and int(raw["max_tokens"]) > 1024:
        raise RoutingError("preflight.max_tokens 不能超过 1024（探针只验证最小生成，不做长输出）")
    # 先校验，再返回原始（未展开）参数；展开由网关的 profile 序列化器统一完成
    serialize_params(profile, raw)
    return raw


# --------------------------------------------------------------------------
# 网关构造（按角色取服务与阶段参数）
# --------------------------------------------------------------------------
def build_gateway(routing: "RoutingPlan", role: str, guard, store, *, template_ref: Optional[Dict[str, Any]] = None):
    """按角色路由构造客户端；mock 只用显式 mock 配置，不静默替代真实服务。"""
    from .clients import MockGateway, OpenAICompatGateway

    route = routing.components.get(role)
    if route is None or not route.enabled:
        raise RoutingError(f"角色 {role} 未启用，不能构造客户端")
    svc = routing.service(route)
    if svc.type == "mock":
        gateway = MockGateway(guard, store, scripts={"*": [{"status": "OK", "text": "{}"}]}, model_name=svc.model or "mock")
        gateway.stage_id = route.stage_id
        gateway.service_id = svc.service_id
        gateway.profile_name = svc.parameter_profile
        return gateway
    gateway = OpenAICompatGateway(
        guard,
        store,
        base_url=svc.base_url,
        model=svc.model,
        api_key_env=svc.api_key_env,
        timeout=float(svc.timeout_s),
        service_id=svc.service_id,
        profile=svc.parameter_profile,
        default_params=routing.raw_stage_params(route.stage_id),
        param_serializer=routing.serializer_for(role),
        auth_required=svc.auth_required,
        template_ref=template_ref or {},
    )
    gateway.stage_id = route.stage_id
    return gateway
