"""可暂停、可编辑、可分支的交互式 episode 驱动器（M2/M3）。

与 ``methods.independent.run_episode`` 使用同一批组件工厂（Engine、Controller、
Assembler、Gateway、BudgetGuard、EventStore），但把主循环外置：
每一步由人或脚本决定「批准 / 编辑后批准 / 跳过 / 从历史节点开分支 / 装配」。

记录格式与在线运行一致（同一事件流、同一 evidence.jsonl/packs.jsonl 增量落盘、
同一收尾写 candidates.jsonl/final.json），因此：
- 独立评价入口（``framework.cli judge``）可直接读取交互会话的运行目录；
- 网页回放无需特判；人工编辑与分支作为 ``query_edited`` / ``branch_started``
  事件保存在流里，原记录、编辑理由、两条分支的消耗都可回看。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .assembly import make_assembler, make_selector
from .budget import BudgetGuard
from .clients import ModelGateway
from .contracts import Action, ActionType, AnswerCandidate, Episode, SessionMode, Task, new_id, stable_hash
from .controller import Controller, ElementController, ScriptedController
from .engine import DEFAULT_SYSTEM, Engine
from .events import EventStore
from .judge import JudgeOnline
from .recovery import RecoveryPolicy
from .routing import build_gateway
from .search import make_policy, make_scorer
from .methods.independent import no_assembly_budget_reason
from .state import make_backend
from .runner import build_run_context, resolve_rubric, write_final, _write_candidates
from .methods.independent import EpisodeArtifacts


def _build_controller(kind, *, queries=None, elements=None, gateway=None, renderer=None,
                        stage_id="plan", template=None, strategy="model_query_v1",
                        strategy_version=""):
    """薄封装：沿用 methods.independent 的控制器构造（单一实现，不复制）。"""
    from .methods.independent import build_controller
    return build_controller(kind, queries=queries, elements=elements, gateway=gateway,
                            renderer=renderer, stage_id=stage_id, template=template,
                            strategy=strategy, strategy_version=strategy_version)


class InteractiveSession:
    """一次可交互的 episode。生命周期：start → (step/approve/edit/branch)* → finalize。"""

    def __init__(
        self,
        cfg: Dict[str, Any],
        task: Task,
        root: str,
        *,
        episode_id: Optional[str] = None,
        ctx=None,
    ):
        if os.path.exists(os.path.join(root, "plan.json")):
            raise FileExistsError(f"运行目录已存在 plan.json：{root}（交互会话不复用既有 root）")
        self.cfg = cfg
        self.task = task
        self.root = root
        exp = cfg.get("experiment", {})
        method_cfg = cfg.get("method", {}) or {}
        routing = ctx.routing if ctx is not None else build_run_context(cfg).routing
        limits = ctx.limits if ctx is not None else None
        if limits is None:
            from .contracts import BudgetLimits
            limits = BudgetLimits(
                target_calls=int((exp.get("budget", {}) or {}).get("target_calls", 20)),
                non_target_calls=int((exp.get("budget", {}) or {}).get("non_target_calls", 400)),
            )
        prompts = ctx.prompts if ctx is not None else None
        if prompts is None and cfg.get("prompts"):
            from .prompts import PromptRegistry
            prompts = PromptRegistry.from_config(cfg)

        self.eid = episode_id or new_id("ep")
        os.makedirs(root, exist_ok=True)
        self._write_plan()

        store = EventStore(root, self.eid)
        guard = BudgetGuard(limits, store, self.eid)

        state_cfg = method_cfg.get("state", {}) or {}
        assembly_cfg = method_cfg.get("assembly", {}) or {}
        search_cfg = method_cfg.get("search", {}) or {}
        controller_cfg = method_cfg.get("controller", {}) or {}

        rubric = resolve_rubric(cfg, task)
        elements = rubric.elements or [
            {"key": e, "accept": [], "critical": True}
            for e in (task.runtime_hints or {}).get("elements", [])
        ]
        backend = make_backend(
            state_cfg.get("backend", "raw_history"), elements,
            max_chars=int(state_cfg.get("max_chars", 400)),
        )

        controller_stage = (routing.components.get("controller").stage_id
                            if routing.components.get("controller") else "plan")
        assembler_stage = (routing.components.get("assembler").stage_id
                           if routing.components.get("assembler") else "assemble")
        self.assembler_stage = assembler_stage

        assembler_template = prompts.get(assembler_stage) if prompts is not None else None
        assembler_gateway = build_gateway(
            routing, "assembler", guard, store,
            template_ref=(assembler_template.ref() if assembler_template else None),
        ) if assembly_cfg.get("type") == "llm" else None
        # citation-relocate@v1：会话启动时 episode 尚无 evidence 行；
        # 装配前由 _refresh_row_texts() 回填（交互过程 target 回复已落盘后）
        assembler = (make_assembler(cfg, gateway=assembler_gateway, template=assembler_template)
                     if assembler_gateway is not None else make_assembler(cfg))

        target_gateway = build_gateway(routing, "target", guard, store)

        system_prompt = str(cfg.get("target_system_prompt", "") or "")

        self.engine = Engine(
            task=task,
            episode=Episode(
                episode_id=self.eid,
                task_id=task.task_id,
                method_version=method_cfg.get("name", "interactive@v0.1"),
                config_hash=stable_hash(cfg),
                budget=limits,
                session_mode=SessionMode(exp.get("session_mode", "stateless")),
                stage_policy_ref=cfg.get("stages", {}).get("ref", ""),
                recovery_policy_version=(method_cfg.get("recovery", {}) or {}).get("version", "r1"),
                seed=int(exp.get("seed", 0)),
            ),
            store=store,
            guard=guard,
            target_gateway=target_gateway,
            state_backend=backend,
            assembler=assembler,
            selector=make_selector(assembly_cfg.get("selector", "first")),
            judge_online=JudgeOnline(),
            rubric=rubric,
            search_policy=make_policy(search_cfg.get("policy", "linear")),
            scorer=make_scorer(search_cfg.get("scorer", "null")),
            elements=elements,
            k_candidates=int(assembly_cfg.get("k", 1)),
            branch_width=int(search_cfg.get("branch_width", 1)),
            stopping_policy=str((method_cfg.get("stopping", {}) or {}).get("policy", "after_assembly")),
            system_prompt=system_prompt,
            target_history_policy=str(state_cfg.get("target_history", "ancestor") or "ancestor").lower(),
            cross_branch_merge=str(assembly_cfg.get("cross_branch_merge", "none") or "none").lower(),
        )
        controller_template = prompts.get(controller_stage) if prompts is not None else None
        controller_gateway = build_gateway(
            routing, "controller", guard, store,
            template_ref=(controller_template.ref() if controller_template else None),
        )
        self.controller = _build_controller(
            str(controller_cfg.get("type", "element")).lower(),
            queries=controller_cfg.get("queries"),
            elements=elements,
            gateway=controller_gateway,
            renderer=backend.render_for_controller,
            stage_id=controller_stage,
            template=controller_template,
            strategy=str(controller_cfg.get("strategy") or "model_query_v1"),
        )
        self.recovery = RecoveryPolicy(store)
        self.store = store
        self.guard = guard
        self.backend = backend
        self.elements = elements
        self.pending: Optional[Action] = None
        self._forced_node: Optional[str] = None
        self.steps = 0
        self.final_reason = ""
        self.engine.start()

    # ------------------------------------------------------------------ 记录
    def _write_plan(self) -> None:
        spec = self.cfg.get("evaluation") or {}
        plan = {
            "experiment_id": self.cfg.get("experiment", {}).get("id", "interactive"),
            "config_hash": stable_hash(self.cfg),
            "n_episodes": 1,
            "dataset": (self.cfg.get("datasets", {}) or {}).get("path", ""),
            "split": (self.cfg.get("datasets", {}) or {}).get("split", "dev"),
            "evaluation_spec": spec,
            "episodes": [{
                "episode_id": self.eid,
                "task": self.task.__dict__,
            }],
            "prompt_manifest": [],
            "note": "interactive session（framework.interactive）",
        }
        with open(os.path.join(self.root, "plan.json"), "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=1)

    # ------------------------------------------------------------------ 步进
    def _current(self) -> str:
        if self._forced_node:
            nid = self._forced_node
            self._forced_node = None
            self.engine.current_node_id = nid
            return nid
        return self.engine.select_node() or self.engine.current_node_id

    def step(self) -> Dict[str, Any]:
        """推进一步：控制器提出动作。ASK_TARGET 挂起等待批准；ASSEMBLE 直接执行。"""
        if self.engine.terminated:
            return {"status": "terminated", "reason": self.engine.termination_reason}
        if self.pending is not None:
            return {"status": "pending_approval", "action": self._action_view(self.pending)}
        node_id = self._current()
        state = self.engine.states[node_id]
        try:
            action = self.controller.propose(
                self.task, state,
                elements=self.elements,
                node_id=node_id,
                view_id=self.engine.nodes[node_id].view_ref,
                budget=self.guard.snapshot(),
                can_assemble=True,
                query_history=self.engine.query_history(node_id),
                candidate_feedback=self.engine.latest_candidate_feedback(),
            )
        except Exception as exc:  # noqa: BLE001 - 控制器失败按事件记录，交上层决定
            self.store.append("action_rejected", role="controller", node_id=node_id,
                              payload={"reason": f"controller_error:{type(exc).__name__}", "detail": str(exc)[:200]})
            self.steps += 1
            return {"status": "controller_error", "detail": str(exc)[:200]}
        if action is None:
            self.steps += 1
            return {"status": "no_action",
                    "note": "控制器未给出动作（额度/格式问题），可 assemble 或继续 step"}
        if getattr(action, "components", None) is not None:
            state.components = [dict(c) for c in action.components]
            self.store.append("components_declared", role="controller", node_id=node_id,
                              payload={"components": [dict(c) for c in action.components],
                                       "action_id": action.action_id})
        if action.type is ActionType.ASK_TARGET:
            self.pending = action
            self.store.append("interactive_paused", role="engine", node_id=node_id,
                              payload={"action_id": action.action_id, "type": "ASK_TARGET",
                                       "query": str(action.params.get("query", ""))[:400],
                                       "note": "等待人工批准/编辑"})
            return {"status": "pending_approval", "action": self._action_view(action)}
        res = self.engine.execute(action)
        self.steps += 1
        if getattr(self.engine, "stop_action_requested", False):
            # R7：Controller 提出 STOP → 统一收尾（装配判断 + 唯一 terminate），
            # 不在这里直接 terminate
            return self.finalize("controller_stop")
        return {"status": "executed", "action_type": action.type.value,
                "ok": res.status.value if hasattr(res.status, "value") else str(res.status)}

    def approve(self, edited_query: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """批准挂起的查询；给出 edited_query 时先落编辑事件再改派。"""
        if self.pending is None:
            return {"status": "nothing_pending"}
        action = self.pending
        self.pending = None
        if edited_query is not None and edited_query.strip():
            original = str(action.params.get("query", ""))
            action.params["query"] = edited_query.strip()
            self.store.append("query_edited", role="human", node_id=action.node_id or "",
                              payload={"action_id": action.action_id, "original": original[:400],
                                       "edited": edited_query.strip()[:400], "reason": reason[:400]})
        res = self.engine.execute(action)
        self.steps += 1
        return {"status": "executed", "action_type": "ASK_TARGET",
                "ok": res.status.value if hasattr(res.status, "value") else str(res.status)}

    def skip_pending(self, reason: str = "") -> Dict[str, Any]:
        if self.pending is None:
            return {"status": "nothing_pending"}
        action = self.pending
        self.pending = None
        self.store.append("action_rejected", role="human", node_id=action.node_id or "",
                          payload={"action_id": action.action_id, "reason": f"user_skipped:{reason}"})
        return {"status": "skipped"}

    def branch_from(self, node_id: str, note: str = "") -> Dict[str, Any]:
        """从历史节点开分支：下一 step 从该节点的视图继续。"""
        if node_id not in self.engine.nodes:
            return {"status": "unknown_node", "known": list(self.engine.nodes)}
        self._forced_node = node_id
        self.store.append("branch_started", role="human", node_id=node_id,
                          payload={"from_node": node_id, "note": note[:400],
                                   "parent_state_seq": self.engine.states[node_id].source_event_seq})
        return {"status": "branched", "from_node": node_id, "note": note}

    def _refresh_row_texts(self) -> None:
        """把 evidence.jsonl 行级文本回填给装配器（引用定位修复消费）。"""
        asm = getattr(self, "assembler", None)
        if asm is not None and getattr(asm, "relocate_meta_rows", False) and self.engine is not None:
            asm.row_text_by_id = {r.get("evidence_id"): (r.get("text") or "")
                                  for r in self.engine.read_evidence_rows()
                                  if isinstance(r, dict) and r.get("evidence_id")}

    def assemble_now(self, reason: str = "user_requested") -> Dict[str, Any]:
        self._refresh_row_texts()
        node_id = self._forced_node or self.engine.current_node_id
        self._forced_node = None
        act = Action(
            action_id=new_id("act"), type=ActionType.ASSEMBLE,
            params={"k": self.engine.k_candidates},
            based_on_state_seq=self.engine.states[node_id].source_event_seq,
            node_id=node_id, view_id=self.engine.nodes[node_id].view_ref,
            rationale=reason,
        )
        self.store.append("action_proposed", role="engine", node_id=node_id,
                          payload={"action_id": act.action_id, "type": "ASSEMBLE", "rationale": reason})
        res = self.engine.execute(act)
        self.steps += 1
        return {"status": "executed", "action_type": "ASSEMBLE",
                "ok": res.status.value if hasattr(res.status, "value") else str(res.status),
                "candidates": len(self.engine.candidates)}

    # ------------------------------------------------------------------ 状态视图
    def _action_view(self, action: Action) -> Dict[str, Any]:
        return {
            "action_id": action.action_id, "type": action.type.value,
            "query": str(action.params.get("query", "")),
            "rationale": action.rationale,
            "node_id": action.node_id, "components": action.components or [],
        }

    def state(self) -> Dict[str, Any]:
        nodes = []
        for nid, node in self.engine.nodes.items():
            st = self.engine.states.get(nid)
            last_material = (st.materials[-1] if st is not None and st.materials else {})
            nodes.append({
                "node_id": nid, "parent": node.parent_node_id,
                "children": [c for c, n2 in self.engine.nodes.items() if n2.parent_node_id == nid],
                "materials": len(st.materials) if st is not None else 0,
                "last_material_excerpt": str(last_material.get("text", ""))[:160],
            })
        return {
            "episode_id": self.eid, "root": self.root,
            "task_id": self.task.task_id, "prompt": self.task.prompt,
            "terminated": self.engine.terminated,
            "termination_reason": self.engine.termination_reason,
            "pending": self._action_view(self.pending) if self.pending else None,
            "current_node": self.engine.current_node_id,
            "steps": self.steps,
            "budget": self.guard.snapshot(),
            "nodes": nodes,
            "packs": [{"pack_id": p.get("pack_id"), "input_hash": p.get("input_hash"),
                       "materials": len(p.get("materials") or [])} for p in self.engine.packs],
            "candidates": [{
                "candidate_id": c.candidate_id, "answer_chars": len(c.answer),
                "claims": len(c.claims), "pack_ids": list(c.evidence_pack_ids),
                "sample_index": (c.assembly_config or {}).get("sample_index"),
            } for c in self.engine.candidates],
            "final_candidate": (self.engine.final_candidate.candidate_id
                                if self.engine.final_candidate else None),
        }

    # ------------------------------------------------------------------ 收尾
    def finalize(self, reason: str = "user_finished") -> Dict[str, Any]:
        """统一收尾：与批处理 run_episode._finalize 同一决策序列。

        1. 无候选，或（online 模式下）材料相对**当前选中候选自己的包**已变化 → 需要装配；
        2. 需要装配且有材料：非 Target 总额度与 Assembler 阶段额度都够 → 模型装配；
           额度不足 → 不调用模型的原文回退（原因按实际缺口记录）；
        3. 无材料且无候选 → 不编造，直接终止；
        4. 候选存在且材料未变（或 after_assembly）→ 保留旧候选，不重复装配。
        连续点击结束：engine.terminate 幂等，终止记录只有一条。
        """
        import time as _time
        self._finalize_started_at = getattr(self, "_finalize_started_at", None) or _time.time()
        if not self.engine.terminated:
            if self.pending is not None:
                # 收尾前丢弃未批准动作：人工未确认的查询不代发
                self.skip_pending("finalize")
            node_id = self.engine.current_node_id
            snap = self.guard.snapshot()
            non_target_left = int(snap.get("non_target_calls_left", 0) or 0)
            _asm_left = self.guard.stage_left(self.assembler_stage)
            asm_stage_left = 1 if _asm_left is None else int(_asm_left)
            has_candidate = self.engine.final_candidate is not None
            # 新鲜度从当前选中候选自己的包算起（Engine 共享助手，与批处理同源）
            materials_changed = (self.engine.candidate_materials_changed(node_id)
                                 if has_candidate else False)
            has_materials = bool(self.engine._build_pack_for(node_id)[0].materials)
            stop_policy = self.engine.stopping_policy
            need_assembly = (not has_candidate) or (stop_policy == "online" and materials_changed)
            if need_assembly and has_materials:
                if non_target_left > 0 and asm_stage_left > 0:
                    try:
                        self.assemble_now("finalize" if not has_candidate else "finalize_new_evidence")
                    except Exception as exc:  # noqa: BLE001 - 与批处理同口径：额度异常走原文回退
                        from .budget import BudgetExhausted
                        if not isinstance(exc, BudgetExhausted):
                            raise
                        _l = self.guard.stage_left(self.assembler_stage)
                        why = no_assembly_budget_reason(
                            int(self.guard.snapshot().get("non_target_calls_left", 0) or 0),
                            1 if _l is None else int(_l))
                        self.store.append(
                            "action_rejected", role="engine", node_id=node_id,
                            payload={"reason": "no_assembly_budget", "detail": why,
                                     "note": "改用不调用模型的原文回退"})
                        self.engine.assemble_without_model(why)
                else:
                    why = no_assembly_budget_reason(non_target_left, asm_stage_left)
                    self.store.append(
                        "action_rejected", role="engine", node_id=node_id,
                        payload={"reason": "no_assembly_budget", "detail": why,
                                 "note": "改用不调用模型的原文回退"})
                    self.engine.assemble_without_model(why)
            elif need_assembly and not has_materials:
                self.store.append(
                    "action_rejected", role="engine", node_id=node_id,
                    payload={"reason": "no_materials_to_assemble",
                             "note": "无材料无候选，按既定规则收尾，不编造答案"})
            elif has_candidate and materials_changed and stop_policy != "online":
                self.store.append(
                    "action_rejected", role="engine", node_id=node_id,
                    payload={"reason": "materials_changed_after_candidate",
                             "note": "非 online 模式保留旧候选"})
            self.engine.terminate(reason)
        art = EpisodeArtifacts(
            episode_id=self.eid, task=self.task, store=self.store, engine=self.engine,
            controller_name=str(getattr(self.controller, "name", "llm")),
            final_candidate=self.engine.final_candidate,
            termination_reason=self.engine.termination_reason, steps=self.steps,
        )
        ep_dir = os.path.join(self.root, "episodes", self.eid)
        _write_candidates(art, os.path.join(ep_dir, "candidates.jsonl"))
        # 尾项a：二次点击结束不抹掉 final.json 已有评价字段（outcome/verdict_id/basis）
        final_path = os.path.join(ep_dir, "final.json")
        prev = {}
        if os.path.exists(final_path):
            try:
                prev = json.load(open(final_path, encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prev = {}
        payload = write_final(
            art, final_path,
            outcome=prev.get("outcome"), verdict_id=prev.get("verdict_id"),
            basis=str(prev.get("basis") or ""),
        )
        # 尾项c：调用分列——整个场景 vs 点击结束后新增（按 request_sent 角色计数）
        def _calls(events):
            out: Dict[str, int] = {}
            for ev in events:
                out[ev.role] = out.get(ev.role, 0) + 1
            return out
        all_req = list(self.store.of_type("request_sent"))
        started = getattr(self, "_finalize_started_at", None)
        payload["calls"] = {
            "scenario_total": _calls(all_req),
            "finalize_added": _calls([e for e in all_req
                                      if started is not None and (getattr(e, "ts", 0) or 0) >= started]),
        }
        json.dump(payload, open(final_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return {"status": "finalized", "episode_id": self.eid,
                "final_candidate": art.final_candidate.candidate_id if art.final_candidate else None,
                "candidates": len(art.engine.candidates),
                "calls": payload["calls"]}


def run_scripted(cfg: Dict[str, Any], task: Task, root: str, script: List[Dict[str, Any]],
                 *, ctx=None) -> Dict[str, Any]:
    """无头驱动交互会话（复现用）。script 为操作序列：

    [{"op": "step"}, {"op": "approve"}, {"op": "approve", "edit": "...", "reason": "..."},
     {"op": "branch", "node": "n1", "note": "..."}, {"op": "assemble"}, {"op": "finalize", "reason": "..."}]
    """
    sess = InteractiveSession(cfg, task, root, ctx=ctx)
    log: List[Dict[str, Any]] = []
    for item in script:
        op = item.get("op")
        if op == "step":
            out = sess.step()
        elif op == "approve":
            out = sess.approve(item.get("edit"), item.get("reason", ""))
        elif op == "skip":
            out = sess.skip_pending(item.get("reason", ""))
        elif op == "branch":
            out = sess.branch_from(item.get("node", ""), item.get("note", ""))
        elif op == "assemble":
            out = sess.assemble_now(item.get("reason", "scripted"))
        elif op == "finalize":
            out = sess.finalize(item.get("reason", "scripted_done"))
            log.append(out)
            return {"log": log, "session": sess}
        else:
            out = {"status": "unknown_op", "op": op}
        log.append(out)
    out = sess.finalize("script_end")
    log.append(out)
    return {"log": log, "session": sess}
