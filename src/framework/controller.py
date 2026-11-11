"""控制器（手册 §4、§12.1）。

Controller 只提出动作，不执行、不记账、不改预算。
- ScriptedController：fixtures 与测试用，按预设查询序列推进；
- ElementController：按任务要求要素的缺项逐项查询（要求要素来自任务声明，不来自参考答案）；
- LLMController：把状态渲染进提示，解析结构化动作；解析失败返回 None 由恢复策略处理。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .clients import ModelGateway
from .contracts import Action, ActionType, AgentState, Role, Status, Task, new_id
from .state import compute_open

ACTION_SCHEMA = """只输出一个 JSON 对象：
{"type": "ASK_TARGET" | "ASSEMBLE" | "STOP", "query": "……", "rationale": "一句话"}
规则：
- 已有足够材料时输出 ASSEMBLE；
- 无新方向可查时输出 STOP；
- query 必须自包含，不能引用"上一条回答"。"""


class Controller:
    name = "base"

    def propose(
        self,
        task: Task,
        state: AgentState,
        *,
        elements: Optional[List[str]] = None,
        node_id: str = "",
        view_id: str = "",
        budget: Optional[Dict[str, Any]] = None,
        can_assemble: bool = True,
    ) -> Optional[Action]:
        raise NotImplementedError

    @staticmethod
    def _mk(type_: ActionType, query: str = "", node_id: str = "", view_id: str = "", seq: int = 0, why: str = "") -> Action:
        return Action(
            action_id=new_id("act"),
            type=type_,
            params={"query": query} if query else {},
            based_on_state_seq=seq,
            node_id=node_id or None,
            view_id=view_id or None,
            rationale=why,
        )


class ScriptedController(Controller):
    """按预设序列提出查询；序列用尽后装配，再停止。"""

    name = "scripted"

    def __init__(self, queries: List[str]):
        self.queries = list(queries)
        self._i = 0
        self._assembled = False

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True):
        if self._i < len(self.queries):
            q = self.queries[self._i]
            self._i += 1
            return self._mk(ActionType.ASK_TARGET, q, node_id, view_id, state.source_event_seq, "scripted")
        if can_assemble and not self._assembled:
            self._assembled = True
            return self._mk(ActionType.ASSEMBLE, "", node_id, view_id, state.source_event_seq, "script_exhausted")
        return self._mk(ActionType.STOP, "", node_id, view_id, state.source_event_seq, "done")


class ElementController(Controller):
    """按缺项查询：每个要求要素问一次，问完装配。"""

    name = "element"

    def __init__(self, elements: List[Any], template: str = "请给出与「{el}」有关的具体信息。"):
        # 兼容字符串与 {"key": ...} 两种要素声明
        self.elements = [e["key"] if isinstance(e, dict) else str(e) for e in (elements or [])]
        self.template = template
        self._asked: set[str] = set()
        self._assembled = False

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True):
        # U04：只读活跃视图（归档不算已知），并把"已问过"与"已满足"分开
        open_goals = {o["goal"] for o in compute_open(state, self.elements)}
        for el in self.elements:
            if el in self._asked or el not in open_goals:
                continue
            self._asked.add(el)
            return self._mk(ActionType.ASK_TARGET, self.template.format(el=el), node_id, view_id, state.source_event_seq, f"missing:{el}")
        if can_assemble and not self._assembled:
            self._assembled = True
            return self._mk(ActionType.ASSEMBLE, "", node_id, view_id, state.source_event_seq, "elements_covered")
        return self._mk(ActionType.STOP, "", node_id, view_id, state.source_event_seq, "done")


class LLMController(Controller):
    """把状态渲染进提示，由本地模型提出动作。解析失败返回 None。"""

    name = "llm"

    def __init__(self, gateway: ModelGateway, renderer, stage_id: str = "plan", temperature: float = 0.7):
        self.gateway = gateway
        self.renderer = renderer
        self.stage_id = stage_id
        self.temperature = temperature

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True):
        prompt = (
            f"原始请求：{task.prompt}\n"
            + (f"必要背景：{task.necessary_context}\n" if task.necessary_context else "")
            + f"\n当前状态：\n{self.renderer(task, state)}\n\n"
            + f"剩余预算：{json.dumps(budget or {}, ensure_ascii=False)}\n\n"
            + ("材料已足够，可以装配。\n" if can_assemble else "")
            + ACTION_SCHEMA
        )
        res = self.gateway.call(
            Role.CONTROLLER,
            self.stage_id,
            [{"role": "user", "content": prompt}],
            {"temperature": self.temperature},
            node_id=node_id,
            view_id=view_id,
        )
        self.last_status = res.status
        self.last_text = res.text
        if not res.ok:
            # 传输/模型层失败：保留真实状态（REFUSED / TIMEOUT / …）
            self.last_failure = res.status
            return None
        data = _extract_json(res.text)
        if not isinstance(data, dict) or data.get("type") not in ("ASK_TARGET", "ASSEMBLE", "STOP"):
            # T02：服务返回 OK 不等于解析出动作；格式失败必须显式进入格式恢复
            self.last_failure = Status.INVALID_OUTPUT
            return None
        if data["type"] == "ASK_TARGET":
            query = data.get("query")
            if not isinstance(query, str) or not query.strip():
                # U03：合法 JSON 但缺必需字段/空查询，同样属于格式失败
                self.last_failure = Status.INVALID_OUTPUT
                return None
        self.last_failure = None
        t = ActionType(data["type"])
        return self._mk(
            t,
            str(data.get("query") or ""),
            node_id,
            view_id,
            state.source_event_seq,
            str(data.get("rationale") or "")[:200],
        )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
