"""plan.v6 组件清单的解析、持久与装配接线（离线替身）。

覆盖链路：LLMController 解析 components → 主循环持久化（components_declared 事件 + state）→
下一次控制器输入携带 → EvidencePack 冻结携带 → 装配输入包含。缺省（scripted/旧提示词）不破坏。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from framework import runner
from framework.assembly import EvidencePack
from framework.controller import _sanitize_components

from test_execution1_r2 import FakeService, routed_cfg, run_once, rows


class ComponentsFake(FakeService):
    """plan 阶段按步数返回带组件清单的动作；assemble 原样走父类。"""

    def __init__(self, *, omit_on_second=False, invalid_on_first=False):
        super().__init__()
        self.omit_on_second = omit_on_second
        self.invalid_on_first = invalid_on_first

    def respond(self, model, text):
        if "JBT_PREFLIGHT" in text:
            return self._ok(model, "OK")
        if model == "unit-target":
            return self._ok(model, json.dumps({"type": "text", "content": "预约首次需要登记；再次无需登记。"}, ensure_ascii=False))
        if "JBT_STAGE=ASSEMBLE" in text:
            payload = {
                "answer": "预约首次需要登记；再次无需登记。",
                "claims": [{"claim_id": "c1", "text": "预约首次需要登记。", "evidence_ids": self._collect_ids(text)}],
                "gaps": [],
            }
            return self._ok(model, json.dumps(payload, ensure_ascii=False))
        if "JBT_STAGE=JUDGE_EVAL" in text:
            return super().respond(model, text)
        # plan 阶段
        self._plans += 1
        good = [
            {"id": "C1", "text": "首次预约的登记条件", "status": "missing"},
            {"id": "C2", "text": "再次预约的免登记条件", "status": "missing"},
        ]
        if self._plans == 1:
            comp = [{"id": "C1", "text": "首次预约的登记条件", "status": "bad_status"}] if self.invalid_on_first else good
            payload = {"type": "ASK_TARGET", "query": "请说明虚构档案室首次预约的登记条件。", "rationale": "锚定组件 C1", "components": comp}
        elif self._plans == 2 and self.omit_on_second:
            payload = {"type": "ASSEMBLE", "rationale": "组件已覆盖"}
        else:
            comp = [
                {"id": "C1", "text": "首次预约的登记条件", "status": "direct"},
                {"id": "C2", "text": "再次预约的免登记条件", "status": "adjacent"},
            ]
            payload = {"type": "ASSEMBLE", "rationale": "组件已覆盖", "components": comp}
        return self._ok(model, json.dumps(payload, ensure_ascii=False))

    def _collect_ids(self, text):
        data = self._input(text)
        ids = []
        for m in (data.get("pack") or {}).get("materials") or []:
            ids.extend(m.get("evidence_ids") or [])
        return ids


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-secret-value")
    f = ComponentsFake()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, *a, **k: f(req))
    return f


def _episode_artifacts(tmp_path, **kw):
    cfg = routed_cfg(tmp_path, budget={"target_calls": 3, "non_target_calls": 14, "stage_calls": {"plan": 6, "assemble": 2}, "tokens": None, "wall_ms": None})
    root, ep, plan, summary = run_once(cfg)
    return root, ep, rows(ep / "events.jsonl")


# --------------------------------------------------------------------------
# 链路：解析 → 持久化 → 控制器输入 → 冻结包 → 装配输入
# --------------------------------------------------------------------------
def test_components_declared_persisted_and_frozen(tmp_path, fake):
    root, ep, events = _episode_artifacts(tmp_path)
    declared = [e for e in events if e["type"] == "components_declared"]
    assert declared, "主循环必须为带 components 的动作落 components_declared 事件"
    payload = declared[-1]["payload"]["components"]
    assert {c["id"] for c in payload} == {"C1", "C2"}
    assert all(c["status"] in ("missing", "adjacent", "direct") for c in payload)
    # 冻结包携带组件清单
    packs = rows(ep / "packs.jsonl")
    assert packs, "packs.jsonl 应有冻结包"
    assert {c["id"] for c in packs[-1]["components"]} == {"C1", "C2"}
    # 装配请求体里包含组件清单（fake 捕获了所有请求）
    asm_bodies = [b for b in fake.bodies if "JBT_STAGE=ASSEMBLE" in json.dumps(b.get("messages", []), ensure_ascii=False)]
    assert asm_bodies, "应有装配请求"
    joined = json.dumps(asm_bodies[-1]["messages"], ensure_ascii=False)
    assert "首次预约的登记条件" in joined, "装配输入必须携带组件清单"


def test_second_controller_input_carries_components(tmp_path, fake):
    _episode_artifacts(tmp_path)
    plan_bodies = [b for b in fake.bodies if "JBT_STAGE=PLAN" in json.dumps(b.get("messages", []), ensure_ascii=False)]
    assert len(plan_bodies) >= 2
    second = json.dumps(plan_bodies[1]["messages"], ensure_ascii=False)
    assert "components" in second, "第二次控制器输入应携带已声明组件清单"
    assert "首次预约的登记条件" in second


def test_omitted_components_keeps_last_declared(tmp_path, monkeypatch):
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-secret-value")
    f = ComponentsFake(omit_on_second=True)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, *a, **k: f(req))
    cfg = routed_cfg(tmp_path, budget={"target_calls": 3, "non_target_calls": 14, "stage_calls": {"plan": 6, "assemble": 2}, "tokens": None, "wall_ms": None})
    root, ep, plan, summary = run_once(cfg)
    events = rows(ep / "events.jsonl")
    declared = [e for e in events if e["type"] == "components_declared"]
    assert len(declared) == 1, "省略 components 的动作不产生新声明，沿用上一份"
    packs = rows(ep / "packs.jsonl")
    assert {c["id"] for c in packs[-1]["components"]} == {"C1", "C2"}, "冻结包仍带首次声明的清单"


def test_invalid_components_treated_as_undeclared(tmp_path, monkeypatch):
    monkeypatch.setenv("JBT_UNIT_KEY", "unit-secret-value")
    f = ComponentsFake(invalid_on_first=True)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, *a, **k: f(req))
    cfg = routed_cfg(tmp_path, budget={"target_calls": 3, "non_target_calls": 14, "stage_calls": {"plan": 6, "assemble": 2}, "tokens": None, "wall_ms": None})
    root, ep, plan, summary = run_once(cfg)
    events = rows(ep / "events.jsonl")
    declared = [e for e in events if e["type"] == "components_declared"]
    # 首动作非法 → 未声明；后续动作（合法清单）正常声明
    assert declared, "合法清单出现后必须有声明"
    first_ids = {c["id"] for c in declared[0]["payload"]["components"]}
    assert first_ids == {"C1", "C2"}


# --------------------------------------------------------------------------
# 单元：清洗器
# --------------------------------------------------------------------------
def test_sanitize_components_rules():
    assert _sanitize_components(None) is None
    assert _sanitize_components("nope") is None
    assert _sanitize_components([{"text": "t", "status": "missing"}]) is not None  # id 可缺省
    # 非法 status → 整体视为未声明
    assert _sanitize_components([{"id": "C1", "text": "t", "status": "maybe"}]) is None
    # 空 text → 整体视为未声明
    assert _sanitize_components([{"id": "C1", "text": "  ", "status": "missing"}]) is None
    out = _sanitize_components([{"id": "C1", "text": "组件", "status": "DIRECT", "extra": "ignored"}])
    assert out == [{"id": "C1", "text": "组件", "status": "direct"}]


def test_pack_without_components_stays_empty(tmp_path):
    pack = EvidencePack(pack_id="p", task_id="t")
    assert pack.components == []
    from framework.assembly import LLMAssembler

    class _T:
        def render(self, payload):
            raise RuntimeError("not called")

        def ref(self):
            return {}

    asm = LLMAssembler.__new__(LLMAssembler)
    # 最小实例直接调用输入构造
    asm2 = LLMAssembler(
        gateway=None,
        template=_T(),
        max_format_retries=0,
        lexical_policy="diagnostic_only",
    )
    out = asm2._input_payload(
        type("T", (), {"public_view": lambda self: {"task_id": "t"}})(),
        pack,
    )
    assert out["pack"]["components"] == [], "未声明组件时装配输入为空列表，不崩溃"
