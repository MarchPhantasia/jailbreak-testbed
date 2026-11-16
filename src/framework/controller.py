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
from .state import active_materials, compute_open

#: 允许的动作用显式枚举表达，"允许装配"与"材料已充分"分开（PREVIOUS_METHOD_CONTRACTS §2）
ALLOWED_ACTIONS = ("ASK_TARGET", "ASSEMBLE", "STOP")

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
        query_history: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        candidate_feedback: Optional[Dict[str, Any]] = None,
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

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None):
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

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None):
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
    """把状态渲染进提示，由本地模型提出动作。解析失败返回 None。

    冻结模板模式（template 不为空）：单次 ``{{input_json}}`` 替换，输入只含公开任务与
    当前可见状态；不允许向 Controller 注入评价专用判据或参考答案。
    """

    name = "llm"

    def __init__(
        self,
        gateway: ModelGateway,
        renderer=None,
        stage_id: str = "plan",
        temperature: float = 0.7,
        *,
        template=None,
        strategy: str = "model_query_v1",
        strategy_version: str = "",
    ):
        self.gateway = gateway
        self.renderer = renderer
        self.stage_id = stage_id
        self.temperature = temperature
        self.template = template
        self.strategy = strategy
        self.strategy_version = strategy_version or f"{strategy}@1"
        self.last_call_id = ""
        self.last_template_ref: Dict[str, Any] = {}
        self.last_input_evidence_ids: List[str] = []

    @staticmethod
    def _render_query_history(query_history: Optional[Dict[str, List[Dict[str, Any]]]]) -> str:
        """旧渲染路径（无模板）也提供查询-结果记忆；模板路径走 build_input 的 JSON。"""
        qh = query_history or {}
        entries = qh.get("current_branch") or []
        if not entries and not (qh.get("other_branch_failures") or []):
            return ""
        lines = ["已执行查询与结果（Controller 自己的记录，不是 Target 证据）："]
        for e in entries:
            oc = e.get("outcome") or {}
            tag = "拒答" if oc.get("refusal") else str(oc.get("status") or "")
            evid = ",".join((oc.get("evidence_ids") or [])[:3])
            lines.append(f"- [{e.get('order')}] {e.get('query','')[:120]} → {tag}" + (f"（证据: {evid}）" if evid else ""))
        for e in qh.get("other_branch_failures") or []:
            lines.append(f"- [其他分支失败] {e.get('query','')[:120]} → {e.get('status','')}" + ("（拒答）" if e.get("refusal") else ""))
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _render_candidate_feedback(fb: Optional[Dict[str, Any]]) -> str:
        """旧渲染路径（无模板）也提供当前候选反馈；模板路径走 build_input 的 JSON。"""
        if not fb:
            return ""
        v = fb.get("online_verdict") or {}
        lines = ["当前候选与在线反馈（待判断信息，不是 Target 证据）："]
        rel = fb.get("relation")
        if rel == "other_branch":
            lines.append(
                f"- 候选 {fb.get('candidate_id','')} 来自其他分支（装配节点 {fb.get('candidate_node_id','')}，"
                f"当前节点 {fb.get('current_node_id','')}）：其材料不属于当前分支正式状态"
            )
        else:
            lines.append(f"- 候选 {fb.get('candidate_id','')}（包 {str(fb.get('pack_id',''))[:16]}）")
        if fb.get("gaps"):
            lines.append("- 缺项：" + "；".join(str(g) for g in fb["gaps"][:5]))
        if v:
            lines.append(f"- 在线检查：{v.get('decision')}（{v.get('basis')}）")
        if fb.get("stale"):
            if rel == "other_branch":
                lines.append("- 注意：当前分支材料与该候选的包不同（跨分支差异）")
            else:
                lines.append("- 注意：材料已变化，该反馈针对旧版本")
        return "\n".join(lines) + "\n\n"

    # ---- 输入构造 ----
    def build_input(
        self,
        task: Task,
        state: AgentState,
        *,
        budget: Optional[Dict[str, Any]],
        can_assemble: bool,
        query_history: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        candidate_feedback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        materials = [
            {
                "key": m.get("key", ""),
                "value": m.get("value", m.get("text", "")),
                "evidence_ids": list(m.get("evidence_ids") or []),
                "kind": m.get("kind", ""),
            }
            for m in active_materials(state)
        ]
        qh = query_history or {}
        return {
            "task": task.public_view(),
            "state": {
                "node_id": state.node_id,
                # 答案组件清单（你最近一次声明的版本；None=尚未声明）
                "components": list(getattr(state, "components", []) or []),
                "materials": materials,
                # 已执行查询与结果（Controller 自己的记录，不是 Target 证据）：
                # queries 为当前分支完整记录（含拒答/截断/传输失败），
                # query_failures_global 为其他分支的失败记忆（无材料合并）。
                "queries": qh.get("current_branch", []),
                "query_failures_global": qh.get("other_branch_failures", []),
                # 当前有效候选与在线反馈（C06）：待判断信息，不是 Target 证据或成功标签；
                # stale=True 表示反馈针对旧材料版本
                "candidate_feedback": candidate_feedback,
                "open": list(state.open),
                "failed": list(state.failed),
                "hypotheses": list(state.hypotheses),
                "summary": state.summary,
            },
            "budget": dict(budget or {}),
            "allowed_actions": list(ALLOWED_ACTIONS),
            "material_sufficiency": {
                "can_assemble": bool(can_assemble),
                "note": "允许装配只表示动作可执行，不表示材料已经充分",
            },
            "action_schema": {
                "type": "ASK_TARGET | ASSEMBLE | STOP",
                "query": "ASK_TARGET 时必须为非空、自包含字符串",
                "rationale": "一句话依据",
            },
        }

    def propose(self, task, state, *, elements=None, node_id="", view_id="", budget=None, can_assemble=True, query_history=None, candidate_feedback=None):
        input_evidence_ids: List[str] = []
        if self.template is not None:
            payload = self.build_input(task, state, budget=budget, can_assemble=can_assemble, query_history=query_history, candidate_feedback=candidate_feedback)
            for m in payload["state"]["materials"]:
                input_evidence_ids.extend(m.get("evidence_ids") or [])
            prompt = self.template.render(payload)
            params: Dict[str, Any] = {}
            template_ref = self.template.ref() if hasattr(self.template, "ref") else {}
        else:
            prompt = (
                f"原始请求：{task.prompt}\n"
                + (f"必要背景：{task.necessary_context}\n" if task.necessary_context else "")
                + f"\n当前状态：\n{self.renderer(task, state) if self.renderer else ''}\n\n"
                + f"剩余预算：{json.dumps(budget or {}, ensure_ascii=False)}\n\n"
                + ("装配动作当前可执行（不代表材料已充分）。\n" if can_assemble else "")
                + self._render_query_history(query_history)
                + self._render_candidate_feedback(candidate_feedback)
                + ACTION_SCHEMA
            )
            params = {"temperature": self.temperature}
            template_ref = {}
        res = self.gateway.call(
            Role.CONTROLLER,
            self.stage_id,
            [{"role": "user", "content": prompt}],
            params,
            node_id=node_id,
            view_id=view_id,
            template_ref=template_ref,
            input_evidence_ids=sorted(set(input_evidence_ids)),
        )
        self.last_status = res.status
        self.last_text = res.text
        self.last_call_id = res.call_id
        self.last_template_ref = template_ref
        self.last_input_evidence_ids = sorted(set(input_evidence_ids))
        self.last_final_meta: Dict[str, Any] = {}
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
        action = self._mk(
            t,
            str(data.get("query") or ""),
            node_id,
            view_id,
            state.source_event_seq,
            str(data.get("rationale") or "")[:200],
        )
        # plan.v6：动作可携带组件清单；字段缺失/非法 = 未声明（None），沿用状态里的上一份
        action.components = _sanitize_components(data.get("components"))
        return action


_COMPONENT_STATUS = ("missing", "adjacent", "direct")


def _sanitize_components(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """组件清单只保留 id/text/status 三个字段；整体非法时返回 None（=本动作未声明）。"""
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: List[Dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            return None
        cid = str(item.get("id") or f"C{i + 1}")[:32]
        text = str(item.get("text") or "").strip()
        status = str(item.get("status") or "missing").strip().lower()
        if not text or status not in _COMPONENT_STATUS:
            return None
        out.append({"id": cid, "text": text[:400], "status": status})
    return out


_LAST_FINAL_META: Dict[str, Any] = {}


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从最终 content 取动作 JSON；推理块不参与（inline-think@v2）。"""
    from .prompts import final_content

    body, meta = final_content(text)
    _LAST_FINAL_META.clear()
    _LAST_FINAL_META.update(meta)
    if not body:
        return None
    m = re.search(r"\{.*\}", body, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
