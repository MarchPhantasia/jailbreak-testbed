"""模型接入层（手册 §9）。

- 所有调用经 ModelGateway，内部强制走 BudgetGuard；
- 只适配已有 HTTP 接口，不负责部署；
- usage 缺失记为 None（unknown），不写成实测 0（R12）；
- 提供 probe() 供 check-services 做真实可达性检查（R01），探针成本单列。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .budget import BudgetGuard, BudgetExhausted
from .contracts import Phase, Role, Status, new_id
from .events import EventStore

#: 旧配置（无 parameter_profile）仍走这个五字段白名单，保持历史序列化不变。
LEGACY_PARAM_KEYS = ("temperature", "top_p", "max_tokens", "stop", "seed")

#: 只属于"传输层"的扩展键：由 profile 序列化器展开到顶层，不直接发送。
TRANSPORT_ONLY_KEYS = ("extra_body",)


@dataclass
class CallResult:
    call_id: str
    status: Status
    text: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    #: 推理文本与最终 content 分开：只有 content 能进入动作/装配/裁判解析
    reasoning: str = ""
    served: bool = True
    from_cache: bool = False
    # S12：来源关联——缓存命中时仍指向最初那次真实调用的响应事件
    response_event_id: str = ""
    source_call_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL)


def usage_field(usage: Dict[str, Any], *names: str) -> Optional[int]:
    """usage 中取字段；缺失返回 None（unknown），与真实 0 区分。"""
    for n in names:
        if n in usage and usage[n] is not None:
            try:
                return int(usage[n])
            except (TypeError, ValueError):
                return None
    return None


class ResponseCache:
    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._store: Dict[str, CallResult] = {}

    @staticmethod
    def key(
        role: str,
        model: str,
        messages: List[Dict[str, str]],
        params: Dict[str, Any],
        *,
        service_id: str = "",
        stage_id: str = "",
        template_sha256: str = "",
    ) -> str:
        """缓存键包含服务/模型/角色/阶段/模板/全部实际参数；不含任何凭证。"""
        return hashlib.sha256(
            json.dumps(
                [role, model, messages, params, service_id, stage_id, template_sha256],
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def get(self, k: str) -> Optional[CallResult]:
        return self._store.get(k) if self.enabled else None

    def put(self, k: str, res: CallResult) -> None:
        if self.enabled:
            self._store[k] = res


class ModelGateway:
    """统一入口：预留 → 发送 → 结算，并保存实际消息与配置。"""

    def __init__(self, guard: BudgetGuard, store: EventStore, cache: Optional[ResponseCache] = None):
        self.guard = guard
        self.store = store
        self.cache = cache or ResponseCache(enabled=False)
        self.model_name = "mock"
        self.calls: List[Dict[str, Any]] = []
        #: 阶段解析后的默认参数；每次调用都与之合并（显式参数优先）
        self.default_params: Dict[str, Any] = {}
        #: profile 序列化器：把 resolved 参数展开为实际 HTTP 顶层字段
        self.param_serializer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
        self.profile_name = ""
        self.template_ref: Dict[str, Any] = {}

    def _merge_params(self, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        merged: Dict[str, Any] = dict(self.default_params or {})
        merged.update({k: v for k, v in (params or {}).items() if v is not None})
        if self.param_serializer is not None:
            return self.param_serializer(merged)
        return {k: v for k, v in merged.items() if k in LEGACY_PARAM_KEYS}

    def call(
        self,
        role: Role | str,
        stage_id: str,
        messages: List[Dict[str, str]],
        params: Optional[Dict[str, Any]] = None,
        *,
        node_id: str = "",
        view_id: str = "",
        attempt_id: str = "",
        phase: Phase = Phase.ONLINE,
        request_id: str = "",
        template_ref: Optional[Dict[str, Any]] = None,
        input_evidence_ids: Optional[List[str]] = None,
    ) -> CallResult:
        role_name = role.value if isinstance(role, Role) else str(role)
        params = self._merge_params(params)
        key = ResponseCache.key(
            role_name,
            self.model_name,
            messages,
            params,
            service_id=getattr(self, "service_id", ""),
            stage_id=stage_id,
            template_sha256=str((template_ref or self.template_ref or {}).get("sha256", "")),
        )
        cached = self.cache.get(key)
        if cached is not None:
            self.store.append(
                "cache_hit",
                phase=phase,
                role=role_name,
                stage_id=stage_id,
                node_id=node_id,
                view_id=view_id,
                attempt_id=attempt_id,
                payload={"key": key, "original_call_id": cached.call_id,
                         "original_response_event_id": cached.response_event_id,
                         "served": False},
            )
            return CallResult(
                call_id=new_id("call"),
                status=cached.status,
                text=cached.text,
                finish_reason=cached.finish_reason,
                usage=dict(cached.usage),
                raw=dict(cached.raw),
                error=cached.error,
                served=False,
                from_cache=True,
                response_event_id=cached.response_event_id,
                source_call_id=cached.call_id,
            )

        ticket = self.guard.reserve(role_name, stage_id=stage_id)
        call_id = new_id("call")
        self.store.append(
            "request_sent",
            phase=phase,
            role=role_name,
            stage_id=stage_id,
            call_id=call_id,
            node_id=node_id,
            view_id=view_id,
            attempt_id=attempt_id,
            request_id=request_id,
            payload={
                "messages": messages,
                "params": params,
                "sent_params": params,
                "resolved_ref": {
                    "stage_id": stage_id,
                    "parameter_profile": self.profile_name,
                    "service_id": getattr(self, "service_id", ""),
                },
                "service_id": getattr(self, "service_id", ""),
                "template_ref": dict(template_ref or self.template_ref or {}),
                "input_evidence_ids": list(input_evidence_ids or []),
                "model": self.model_name,
                "request_id": request_id,
                "served": True,
                "ticket": ticket,
            },
        )
        self.guard.dispatch(ticket)
        t0 = time.time()
        try:
            res = self._invoke(role_name, stage_id, messages, params)
            res.call_id = call_id
        except Exception as exc:  # noqa: BLE001
            res = CallResult(call_id=call_id, status=Status.TRANSPORT_ERROR, error=str(exc))
        wall_ms = int((time.time() - t0) * 1000)
        cost = {
            "tokens_in": usage_field(res.usage, "prompt_tokens", "input_tokens"),
            "tokens_out": usage_field(res.usage, "completion_tokens", "output_tokens"),
            "wall_ms": wall_ms,
            "usage_status": "reported" if res.usage else "unknown",
        }
        response_ev = self.store.append(
            "response_received",
            phase=phase,
            role=role_name,
            stage_id=stage_id,
            call_id=call_id,
            node_id=node_id,
            view_id=view_id,
            attempt_id=attempt_id,
            request_id=request_id,
            payload={
                "status": res.status.value,
                "text": res.text,
                "reasoning": res.reasoning,
                "finish_reason": res.finish_reason,
                "usage": res.usage,
                "usage_status": cost["usage_status"],
                "error": res.error,
                "raw": res.raw,
            },
        )
        self.guard.settle(ticket, cost, call_id=call_id)
        res.response_event_id = response_ev.event_id if response_ev is not None else ""
        self.cache.put(key, res)
        self.calls.append(
            {
                "role": role_name,
                "stage": stage_id,
                "messages": messages,
                "params": params,
                "status": res.status.value,
                "model": self.model_name,
                "call_id": call_id,
                "phase": phase.value if isinstance(phase, Phase) else str(phase),
            }
        )
        return res

    def _invoke(self, role: str, stage: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> CallResult:  # pragma: no cover
        raise NotImplementedError

    def probe(self) -> Dict[str, Any]:  # pragma: no cover - 具体客户端实现
        raise NotImplementedError


class MockGateway(ModelGateway):
    """脚本化客户端：测试与 fixtures 用，不联网。"""

    def __init__(self, guard: BudgetGuard, store: EventStore, scripts: Optional[Dict[str, Any]] = None, cache: Optional[ResponseCache] = None, model_name: str = "mock"):
        super().__init__(guard, store, cache)
        self.model_name = model_name
        self.scripts = scripts or {"*": [CallResult(call_id="", status=Status.OK, text="ok")]}
        self._cursor: Dict[str, int] = {}

    def _invoke(self, role: str, stage: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> CallResult:
        last_user = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break
        key = "*"
        for cand in self.scripts:
            if cand != "*" and cand in last_user:
                key = cand
                break
        seq = self.scripts.get(key) or self.scripts.get("*")
        if seq is None:
            return CallResult(call_id="", status=Status.INVALID_OUTPUT, error=f"no script for {key}")
        idx = self._cursor.get(key, 0)
        self._cursor[key] = idx + 1
        item = seq[idx] if idx < len(seq) else seq[-1]
        res = item(messages) if callable(item) else item
        return CallResult(
            call_id="",
            status=res.status,
            text=res.text,
            finish_reason=res.finish_reason,
            usage=dict(res.usage),
            raw=dict(res.raw),
            reasoning=res.reasoning,
            error=res.error,
        )

    def probe(self) -> Dict[str, Any]:
        return {"ok": True, "type": "mock", "note": "mock 不产生真实调用"}


class OpenAICompatGateway(ModelGateway):
    """OpenAI 兼容 /v1/chat/completions 的薄客户端（urllib，无第三方依赖）。"""

    def __init__(
        self,
        guard: BudgetGuard,
        store: EventStore,
        base_url: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: float = 60.0,
        cache: Optional[ResponseCache] = None,
        *,
        service_id: str = "",
        profile: str = "",
        default_params: Optional[Dict[str, Any]] = None,
        param_serializer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        auth_required: bool = False,
        template_ref: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(guard, store, cache)
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.service_id = service_id
        self.profile_name = profile
        self.default_params = dict(default_params or {})
        self.param_serializer = param_serializer
        self.auth_required = bool(auth_required)
        self.template_ref = dict(template_ref or {})
        if self.auth_required:
            # 调用前显式失败：不把空凭证当成"未鉴权服务"，也不发一次注定失败的请求
            self._api_key()

    def _api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if self.auth_required and not (key or "").strip():
            # 调用前显式失败：不把空凭证当成"未鉴权服务"
            raise ValueError(f"服务要求认证，但环境变量 {self.api_key_env} 缺失或为空")
        return key or ""

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key.strip():
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _invoke(self, role: str, stage: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> CallResult:
        # params 已由 profile 序列化器展开并校验过；这里不再做静默丢弃
        body: Dict[str, Any] = {"model": self.model_name, "messages": messages, "stream": False}
        for key, value in params.items():
            if key in TRANSPORT_ONLY_KEYS:
                continue
            body[key] = value
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return CallResult(call_id="", status=Status.TRANSPORT_ERROR, error=f"HTTP {exc.code}")
        except TimeoutError:
            return CallResult(call_id="", status=Status.TIMEOUT, error="timeout")
        except Exception as exc:  # noqa: BLE001
            return CallResult(call_id="", status=Status.TRANSPORT_ERROR, error=str(exc))

        return self._parse_completion(data)

    @staticmethod
    def _parse_completion(data: Any) -> CallResult:
        """把供应商响应映射到接口状态（V02）。

        - 缺 choices / 结构非法 → INVALID_OUTPUT（不能冒充 OK 空串）
        - message.refusal 且无内容 → REFUSED（保留拒答文本与原因）
        - finish_reason=content_filter 且无内容 → REFUSED（原因记为 content_filter）
        - 有内容又带 refusal → PARTIAL（部分可用）
        - finish_reason=length → TRUNCATED；其余有内容 → OK
        """
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
            return CallResult(call_id="", status=Status.INVALID_OUTPUT, error="malformed_response:no_choices")
        choice = data["choices"][0]
        if not isinstance(choice, dict):
            return CallResult(call_id="", status=Status.INVALID_OUTPUT, error="malformed_response:choice_not_object")
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        content = message.get("content") or ""
        refusal = message.get("refusal") or ""
        # vLLM 不同版本用 reasoning 或旧名 reasoning_content；两者都不等于最终 content
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        if isinstance(reasoning, list):  # 少数服务把推理分段返回
            reasoning = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in reasoning)
        finish = choice.get("finish_reason") or ""
        usage = data.get("usage") or {}
        raw = {"id": data.get("id"), "model": data.get("model"), "finish_reason": finish}
        if refusal:
            raw["refusal"] = refusal
        if finish == "content_filter":
            raw["filter_reason"] = "content_filter"

        if content and refusal:
            status, error = Status.PARTIAL, "mixed_refusal_with_content"
        elif content:
            status = Status.TRUNCATED if finish == "length" else Status.OK
            error = ""
        elif refusal:
            status, error = Status.REFUSED, "provider_refusal"
        elif finish == "content_filter":
            status, error = Status.REFUSED, "content_filter"
        elif reasoning:
            # 只有推理、没有最终 content：不是可用结果（M07），也不能当成功
            status, error = Status.INVALID_OUTPUT, "reasoning_only"
        else:
            status, error = Status.INVALID_OUTPUT, "empty_content"

        return CallResult(
            call_id="",
            status=status,
            text=content or refusal,
            finish_reason=finish,
            usage=usage,
            raw=raw,
            reasoning=str(reasoning),
            error=error,
        )

    def probe(self) -> Dict[str, Any]:
        """真实可达性检查：GET /models；失败即不健康。"""
        req = urllib.request.Request(f"{self.base_url}/models", headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=min(10.0, self.timeout)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return {"ok": True, "type": "openai", "models": [m.get("id") for m in (data.get("data") or [])][:5]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "type": "openai", "error": str(exc)}


def probe_openai(base_url: str, api_key_env: str = "OPENAI_API_KEY", timeout: float = 10.0) -> Dict[str, Any]:
    """独立探针：不占用 episode 预算，结果写入 preflight（R01）。"""
    if not base_url:
        return {"ok": False, "error": "empty base_url"}
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {os.environ.get(api_key_env, '')}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("id") for m in (data.get("data") or [])][:5]
        return {"ok": True, "models": models}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
