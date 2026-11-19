"""上下文预算估算与预检（不依赖具体 tokenizer）。

服务不会告诉我们它的 tokenizer；这里用保守估算做**发送前**检查，避免把必然超窗的请求
发出去浪费调用。真实上限仍以服务端为准，估算值与判定结果都会记录在事件里。

估算规则（偏保守）：
- CJK 字符按 1 token/字；
- 其他字符按 0.3 token/字符（≈3.3 字符/token）；
- 每条消息再加 4 token 结构开销。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
ESTIMATOR_VERSION = "cjk-aware@v1"
MESSAGE_OVERHEAD = 4
DEFAULT_RESERVE = 512


def estimate_tokens(text: str) -> int:
    """保守估算一段文本的 token 数；空串为 0。"""
    body = str(text or "")
    if not body:
        return 0
    cjk = len(CJK_RE.findall(body))
    other = len(body) - cjk
    return int(cjk * 1.0 + other * 0.3 + 0.999)


def messages_tokens(messages: Iterable[Dict[str, Any]]) -> int:
    return sum(estimate_tokens(m.get("content", "")) + MESSAGE_OVERHEAD for m in messages)


def request_budget(
    *,
    context_window: Optional[int],
    messages: List[Dict[str, Any]],
    max_tokens: Optional[int],
    reserve: int = DEFAULT_RESERVE,
) -> Dict[str, Any]:
    """返回发送前预算快照；context_window 未知时只报告估算值。"""
    input_tokens = messages_tokens(messages)
    output_tokens = int(max_tokens or 0)
    window = int(context_window) if context_window else None
    info: Dict[str, Any] = {
        "estimator": ESTIMATOR_VERSION,
        "context_window": window,
        "estimated_input_tokens": input_tokens,
        "max_tokens": output_tokens,
        "reserve_tokens": reserve,
        "fits": None,
        "headroom_tokens": None,
    }
    if window:
        headroom = window - input_tokens - output_tokens - reserve
        info["headroom_tokens"] = headroom
        info["fits"] = headroom >= 0
    return info


def input_token_budget(context_window: Optional[int], max_tokens: Optional[int], reserve: int = DEFAULT_RESERVE) -> Optional[int]:
    """可用于输入的 token 预算；上下文未知时返回 None。"""
    if not context_window:
        return None
    return max(0, int(context_window) - int(max_tokens or 0) - reserve)


