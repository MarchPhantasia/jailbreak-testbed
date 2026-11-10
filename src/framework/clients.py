"""模型接入层（手册 §9）。

- 所有调用经 ModelGateway，内部强制走 BudgetGuard；
- 只适配已有 HTTP 接口，不负责部署（权重、引擎、GPU 在仓库之外）；
- 保存实际发送的消息、配置、原始文本、finish_reason 与用量；
- 缓存默认关闭（正式在线实验），开启时记录 cache_hit 且 served=False。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .budget import BudgetGuard, BudgetExhausted
from .contracts import Role, Status, new_id, stable_hash
from .events import EventStore


@dataclass
class CallResult:
    call_id: str
    status: Status
    text: str = ""
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    served: bool = True
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.PARTIAL)


class ResponseCache:
    """内容哈希缓存。默认关闭；开启时它只减少 served 调用，不产生新样本。"""

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._store: Dict[str, CallResult] = {}

    @staticmethod
    def key(role: str, model: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps([role, model, messages, params], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
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

    def call(
        self,
        role: Role | str,
        stage_id: str,
        messages: List[Dict[str, str]],
        params: Optional[Dict[str, Any]] = None,
        *,
        node_id: str = "",
        view_id: str = "",
    ) -> CallResult:
        role_name = role.value if isinstance(role, Role) else str(role)
        params = params or {}
        key = ResponseCache.key(role_name, self.model_name, messages, params)
        cached = self.cache.get(key)
        if cached is not None:
            self.store.append(
                "cache_hit",
                role=role_name,
                stage_id=stage_id,
                node_id=node_id,
                view_id=view_id,
                payload={"key": key},
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
            )

        ticket = self.guard.reserve(role_name)
        call_id = new_id("call")
        self.store.append(
            "request_sent",
            role=role_name,
            stage_id=stage_id,
            call_id=call_id,
            node_id=node_id,
            view_id=view_id,
            payload={
                "messages": messages,
                "params": params,
                "model": self.model_name,
                "served": True,
                "ticket": ticket,
            },
        )
        self.guard.dispatch(ticket)
        t0 = time.time()
        try:
            res = self._invoke(role_name, stage_id, messages, params)
            res.call_id = call_id
        except Exception as exc:  # noqa: BLE001  传输层异常统一降级
            res = CallResult(call_id=call_id, status=Status.TRANSPORT_ERROR, error=str(exc))
        wall_ms = int((time.time() - t0) * 1000)
        cost = {
            "tokens_in": int(res.usage.get("prompt_tokens") or res.usage.get("input_tokens") or 0),
            "tokens_out": int(res.usage.get("completion_tokens") or res.usage.get("output_tokens") or 0),
            "wall_ms": wall_ms,
        }
        self.store.append(
            "response_received",
            role=role_name,
            stage_id=stage_id,
            call_id=call_id,
            node_id=node_id,
            view_id=view_id,
            payload={
                "status": res.status.value,
                "text": res.text,
                "finish_reason": res.finish_reason,
                "usage": res.usage,
                "error": res.error,
                "raw": res.raw,
            },
        )
        self.guard.settle(ticket, cost, call_id=call_id)
        self.cache.put(key, res)
        self.calls.append({"role": role_name, "stage": stage_id, "messages": messages, "params": params, "status": res.status.value})
        return res

    def _invoke(self, role: str, stage: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> CallResult:  # pragma: no cover
        raise NotImplementedError


class MockGateway(ModelGateway):
    """脚本化客户端：测试与 fixtures 用，不联网。

    scripts: {matcher: [CallResult | callable(messages)->CallResult, ...]}
    matcher 为字符串时按子串匹配（在最后一条 user 消息里），* 为默认。
    """

    def __init__(self, guard: BudgetGuard, store: EventStore, scripts: Optional[Dict[str, Any]] = None, cache: Optional[ResponseCache] = None):
        super().__init__(guard, store, cache)
        self.model_name = "mock"
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
        if callable(item):
            res = item(messages)
        else:
            res = CallResult(
                call_id="",
                status=item.status,
                text=item.text,
                finish_reason=item.finish_reason,
                usage=dict(item.usage),
                raw=dict(item.raw),
                error=item.error,
            )
        res.served = True
        return res


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
    ):
        super().__init__(guard, store, cache)
        self.base_url = base_url.rstrip("/")
        self.model_name = model
        self.api_key_env = api_key_env
        self.timeout = timeout

    def _invoke(self, role: str, stage: str, messages: List[Dict[str, str]], params: Dict[str, Any]) -> CallResult:
        body = {"model": self.model_name, "messages": messages}
        body.update({k: v for k, v in params.items() if k in ("temperature", "top_p", "max_tokens", "stop", "seed")})
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ.get(self.api_key_env, '')}",
            },
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

        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content", "") or ""
        finish = choice.get("finish_reason", "") or ""
        status = Status.TRUNCATED if finish == "length" else Status.OK
        return CallResult(
            call_id="",
            status=status,
            text=text,
            finish_reason=finish,
            usage=data.get("usage") or {},
            raw={"id": data.get("id"), "model": data.get("model")},
        )
