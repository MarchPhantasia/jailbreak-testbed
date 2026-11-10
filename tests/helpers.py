"""测试用的最小构件。"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from framework.assembly import Assembler, make_selector
from framework.budget import BudgetGuard
from framework.clients import CallResult, MockGateway, ResponseCache
from framework.contracts import (
    AgentState,
    AnswerCandidate,
    BudgetLimits,
    Episode,
    Status,
    Task,
    stable_hash,
)
from framework.controller import ElementController, ScriptedController
from framework.engine import Engine
from framework.events import EventStore
from framework.judge import JudgeOnline, Rubric


def make_task(task_id: str = "t1", prompt: str = "整理资料", elements: Optional[List[str]] = None, scripts: Optional[Dict[str, Any]] = None, context: str = "") -> Task:
    return Task(
        task_id=task_id,
        prompt=prompt,
        primary_dataset="synthetic",
        necessary_context=context,
        runtime_hints={
            "elements": elements or ["库位", "登记", "开放时间"],
            "target_scripts": scripts or {},
        },
    )


def scripted(entries: Dict[str, List[Dict[str, str]]]) -> Dict[str, List[CallResult]]:
    out: Dict[str, List[CallResult]] = {}
    for k, v in entries.items():
        out[k] = [CallResult(call_id="", status=Status(e.get("status", "OK")), text=e.get("text", "")) for e in v]
    return out


def make_engine(
    tmp_path,
    *,
    task: Optional[Task] = None,
    scripts: Optional[Dict[str, Any]] = None,
    target_calls: int = 4,
    backend: str = "fox",
    cache: Optional[ResponseCache] = None,
    k: int = 1,
    policy: str = "linear",
    scorer: str = "rule",
    episode_id: str = "ep_test",
):
    from framework.state import make_backend
    from framework.search import make_policy, make_scorer

    task = task or make_task()
    store = EventStore(str(tmp_path), episode_id)
    limits = BudgetLimits(target_calls=target_calls, non_target_calls=50)
    guard = BudgetGuard(limits, store, episode_id)
    gw = MockGateway(guard, store, scripts=scripted(scripts or {"*": [{"text": "- 库位: C04\n"}]}), cache=cache)
    elements = task.runtime_hints.get("elements") or []
    rubric = Rubric(rubric_id="r1", elements=[{"key": e, "critical": True, "accept": [e]} for e in elements])
    engine = Engine(
        task=task,
        episode=Episode(
            episode_id=episode_id,
            task_id=task.task_id,
            method_version="test@v1",
            config_hash=stable_hash({"t": 1}),
            budget=limits,
        ),
        store=store,
        guard=guard,
        target_gateway=gw,
        state_backend=make_backend(backend, elements),
        assembler=Assembler(),
        selector=make_selector("coverage"),
        judge_online=JudgeOnline(),
        rubric=rubric,
        search_policy=make_policy(policy),
        scorer=make_scorer(scorer),
        elements=elements,
        k_candidates=k,
    )
    engine.start()
    return engine, store, guard, gw
