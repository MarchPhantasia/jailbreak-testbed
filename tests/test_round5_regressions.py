"""第五轮审查 V01–V02 的回归测试。"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from framework.clients import OpenAICompatGateway
from framework.contracts import ActionType, Role, Status
from framework.controller import Controller
from framework.methods.independent import run_episode

from helpers import make_engine, make_task
from test_runner_e2e import make_cfg


# --------------------------------------------------------------------------
# V01：online 继续取证后，自动收尾必须消费最新材料
# --------------------------------------------------------------------------
class _Sequence(Controller):
    name = "sequence"

    def __init__(self):
        self.i = 0

    def propose(self, task, state, **kw):
        seq = [
            (ActionType.ASK_TARGET, "q1"),
            (ActionType.ASSEMBLE, ""),
            (ActionType.ASK_TARGET, "q2"),
            (ActionType.ASK_TARGET, "q3"),
        ]
        action, query = seq[min(self.i, len(seq) - 1)]
        self.i += 1
        return self._mk(action, query, kw["node_id"], kw["view_id"], state.source_event_seq, "test")


@pytest.mark.parametrize("boundary", ["max_steps", "target_budget"])
def test_V01_online_finalization_uses_new_evidence(tmp_path, monkeypatch, boundary):
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **kw: _Sequence())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["budget"]["target_calls"] = 2
    cfg["experiment"]["max_steps"] = 3 if boundary == "max_steps" else 8
    task = make_task(elements=["库位", "登记"], scripts={"*": [{"text": "- 库位: C04"}, {"text": "- 登记: 首次需要登记"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "run"))
    answer = art.final_candidate.answer if art.final_candidate else ""
    assert art.engine.guard.target_calls_used() == 2
    assert art.store.count("terminate") == 1
    assert "首次需要登记" in answer, "online 取到新证据后仍冻结了旧候选"


def test_V01_no_change_does_not_reassemble_at_finalize(tmp_path, monkeypatch):
    """收尾时材料未变化 → 不重复装配（max_steps 恰好停在首次装配之后）。"""

    class Once(Controller):
        name = "once"

        def __init__(self):
            self.i = 0

        def propose(self, task, state, **kw):
            self.i += 1
            if self.i == 1:
                return self._mk(ActionType.ASK_TARGET, "q1", kw["node_id"], kw["view_id"], state.source_event_seq)
            return self._mk(ActionType.ASSEMBLE, "", kw["node_id"], kw["view_id"], state.source_event_seq)

    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **kw: Once())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["max_steps"] = 2
    art = run_episode(make_task(scripts={"*": [{"text": "- 库位: C04"}]}), config=cfg, results_root=str(tmp_path / "r"))
    assert art.store.count("assembled") == 1
    assert art.final_candidate is not None


def test_V01_old_candidate_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr("framework.methods.independent.build_controller", lambda *a, **kw: _Sequence())
    cfg = make_cfg(tmp_path)
    cfg["method"]["stopping"] = {"policy": "online"}
    cfg["experiment"]["budget"]["target_calls"] = 2
    task = make_task(elements=["库位", "登记"], scripts={"*": [{"text": "- 库位: C04"}, {"text": "- 登记: 首次需要登记"}]})
    art = run_episode(task, config=cfg, results_root=str(tmp_path / "run"))
    assert len(art.engine.candidates) >= 2
    first = art.engine.candidates[0].answer
    assert "首次需要登记" not in first  # 旧候选保持原样
    assert "首次需要登记" in art.final_candidate.answer


# --------------------------------------------------------------------------
# V02：HTTP 响应语义
# --------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.data).encode("utf-8")


def _call_with(payload, tmp_path):
    engine, store, guard, _ = make_engine(tmp_path)
    gw = OpenAICompatGateway(guard, store, "http://unit.invalid/v1", "fixture")
    with patch("framework.clients.urllib.request.urlopen", return_value=_FakeResponse(payload)):
        res = gw.call(Role.CONTROLLER, "plan", [{"role": "user", "content": "x"}])
    return res, guard


@pytest.mark.parametrize(
    "payload,expected,reason",
    [
        ({"choices": [{"message": {"content": None, "refusal": "I cannot assist."}, "finish_reason": "stop"}]}, Status.REFUSED, "provider_refusal"),
        ({"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}, Status.REFUSED, "content_filter"),
        ({"id": "x", "object": "chat.completion"}, Status.INVALID_OUTPUT, "malformed_response:no_choices"),
    ],
)
def test_V02_http_semantics(tmp_path, payload, expected, reason):
    res, guard = _call_with(payload, tmp_path)
    assert res.status is expected
    assert res.error == reason
    assert guard.snapshot()["non_target_calls_used"] == 1  # 已发出即结算


def test_V02_normal_content_ok(tmp_path):
    res, _ = _call_with({"choices": [{"message": {"content": "- 库位: C04"}, "finish_reason": "stop"}]}, tmp_path)
    assert res.status is Status.OK and "C04" in res.text


def test_V02_mixed_refusal_with_content_is_partial(tmp_path):
    res, _ = _call_with(
        {"choices": [{"message": {"content": "- 库位: C04", "refusal": "partial"}, "finish_reason": "stop"}]}, tmp_path
    )
    assert res.status is Status.PARTIAL


def test_V02_truncated_keeps_reason(tmp_path):
    res, _ = _call_with({"choices": [{"message": {"content": "partial answer"}, "finish_reason": "length"}]}, tmp_path)
    assert res.status is Status.TRUNCATED


def test_V02_filter_reason_is_tagged(tmp_path):
    res, _ = _call_with({"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}, tmp_path)
    assert res.raw.get("filter_reason") == "content_filter"
