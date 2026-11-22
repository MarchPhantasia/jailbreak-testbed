"""judge-retry@v2 验收测试：失败分流后的实际消息与记账。

修复前反例：504 后重试的消息被追加上次输出+格式提醒（1→3 条），送评输入改变。
修复后：暂时性错误同消息重发（哈希不变）；仅解析失败附加修复（哈希改变）。
替身服务覆盖：504→合法判定；不可解析正文→合法判定；重试余额 0；预检拦截；
合法 unscored；修复请求随后遇 504；缓存开启仍真实重发。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from framework.contracts import Role, Status  # noqa: E402
from framework.evaluation import (  # noqa: E402
    JUDGE_RETRY_RULE_VERSION, SemanticJudge, classify_judge_failure,
)


def _ok_content():
    return json.dumps({
        "assessment": "scored", "task_fulfillment": "complete",
        "source_compliance": "met", "evidence_sufficiency": "sufficient",
        "reason": "ok"}, ensure_ascii=False)


class FakeCallResult:
    def __init__(self, status, text="", error="", call_id="call_x", finish_reason="stop"):
        self.status = status if isinstance(status, Status) else Status(status)
        self.text = text
        self.error = error
        self.call_id = call_id
        self.finish_reason = finish_reason
        self.ok = self.status is Status.OK

    @property
    def tokens_in(self):
        return 10

    @property
    def tokens_out(self):
        return 5


class FakeGateway:
    """按脚本回放结果；记录每次实际发送的 messages。"""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def call(self, role, stage_id, messages, params, **kw):
        self.sent.append({"role": role, "messages": [dict(m) for m in messages],
                          "params": dict(params), "kw": dict(kw)})
        return self.script.pop(0)


def _judge(gateway, max_format_retries=1):
    from framework.prompts import PromptTemplate
    tpl = PromptTemplate("judge_eval", "judge-semantic-test", "1", "judge_eval",
                         "JBT_STAGE=JUDGE_EVAL\n{{input_json}}", "")
    return SemanticJudge(gateway, tpl, rubric={
        "success_rule": {"task_fulfillment": "complete", "source_compliance": "met",
                         "evidence_sufficiency": "sufficient"}},
        max_format_retries=max_format_retries)


def _task_candidate():
    from framework.contracts import AnswerCandidate, Task
    task = Task(task_id="t1", prompt="原始任务", primary_dataset="synthetic")
    cand = AnswerCandidate(candidate_id="c1", answer="答案", claims=[], evidence_pack_ids=[])
    return task, cand


# ---------------- 分流分类器 ----------------

def test_classify_judge_failure_matrix():
    assert classify_judge_failure("TRANSPORT_ERROR", "HTTP 504", False) == ("transient", True, False)
    assert classify_judge_failure("TRANSPORT_ERROR", "HTTP 503", False)[0] == "transient"
    assert classify_judge_failure("TIMEOUT", "", False)[0] == "transient"
    assert classify_judge_failure("TRANSPORT_ERROR", "HTTP 401", False) == ("terminal", False, False)
    assert classify_judge_failure("REFUSED", "", True) == ("terminal", False, False)
    assert classify_judge_failure("TRANSPORT_ERROR", "HTTP 504", True)[:2] == ("transient", True)
    assert classify_judge_failure("OK", "", True) == ("parse", True, True)
    assert classify_judge_failure("INVALID_OUTPUT", "", False) == ("empty", True, False)
    assert classify_judge_failure("TRUNCATED", "", True) == ("truncated", True, False)


# ---------------- 504 → 合法判定（同消息重发） ----------------

def test_504_then_valid_same_messages():
    gw = FakeGateway([
        FakeCallResult("TRANSPORT_ERROR", error="HTTP 504"),
        FakeCallResult("OK", text=_ok_content()),
    ])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e1")
    assert v.usable and v.payload.get("task_fulfillment") == "complete"
    assert len(gw.sent) == 2
    # 前一组两次消息哈希相同：消息逐条一致
    assert gw.sent[0]["messages"] == gw.sent[1]["messages"]
    assert all(len(s["messages"]) == 1 for s in gw.sent)
    # attempts 记录哈希与失败分类
    at = v.payload.get("attempts") or j_last_attempts(j)
    assert at[0]["failure_kind"] == "transient"
    assert at[0]["messages_hash"] == at[1]["messages_hash"]
    assert at[0]["retry_rule"] == JUDGE_RETRY_RULE_VERSION
    # 重试走真实派发（cache_bypass）
    assert gw.sent[1]["kw"].get("cache_bypass") is True
    assert gw.sent[0]["kw"].get("cache_bypass") is False


def j_last_attempts(judge):
    raise AssertionError("verdict payload should carry attempts")


# ---------------- 不可解析正文 → 合法判定（消息改变） ----------------

def test_unparseable_then_valid_messages_change():
    gw = FakeGateway([
        FakeCallResult("OK", text="这不是 JSON"),
        FakeCallResult("OK", text=_ok_content()),
    ])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e2")
    assert v.usable
    assert len(gw.sent) == 2
    assert len(gw.sent[0]["messages"]) == 1
    assert len(gw.sent[1]["messages"]) == 3  # 原提示 + 上次输出 + 修复指令
    at = v.payload["attempts"]
    assert at[0]["failure_kind"] == "parse"
    assert at[0]["messages_hash"] != at[1]["messages_hash"]


# ---------------- 空正文（仅推理）→ 同消息重试 ----------------

def test_empty_text_same_message_retry():
    gw = FakeGateway([
        FakeCallResult("INVALID_OUTPUT", text="", error="reasoning_only"),
        FakeCallResult("OK", text=_ok_content()),
    ])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e3")
    assert v.usable
    assert gw.sent[0]["messages"] == gw.sent[1]["messages"]
    at = v.payload["attempts"]
    assert at[0]["failure_kind"] == "empty"


# ---------------- 余额为 0：一次失败即 U ----------------

def test_no_retry_budget_single_attempt():
    gw = FakeGateway([
        FakeCallResult("TRANSPORT_ERROR", error="HTTP 504"),
    ])
    j = _judge(gw, max_format_retries=0)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e4")
    assert not v.usable
    assert "TRANSPORT_ERROR" in v.basis
    assert len(gw.sent) == 1


# ---------------- 终态错误不重试 ----------------

def test_auth_error_no_retry():
    gw = FakeGateway([
        FakeCallResult("TRANSPORT_ERROR", error="HTTP 401"),
    ])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e5")
    assert not v.usable and len(gw.sent) == 1
    assert v.payload["attempts"][0]["failure_kind"] == "terminal"


def test_refused_no_retry():
    gw = FakeGateway([FakeCallResult("REFUSED")])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e6")
    assert not v.usable and len(gw.sent) == 1


# ---------------- 合法 unscored：不重试、计 U ----------------

def test_legal_unscored_counts_returned():
    gw = FakeGateway([FakeCallResult("OK", text=json.dumps({
        "assessment": "unscored", "reason": "材料不足"}, ensure_ascii=False))])
    j = _judge(gw)
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e7")
    assert not v.usable and "judge_declined" in v.basis
    assert len(gw.sent) == 1


# ---------------- 修复请求随后遇 504：共享余额，如实 U ----------------

def test_repair_then_504_shared_budget():
    gw = FakeGateway([
        FakeCallResult("OK", text="坏 JSON"),
        FakeCallResult("TRANSPORT_ERROR", error="HTTP 504"),
    ])
    j = _judge(gw)  # 1 + 1 = 2 次上限
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e8")
    assert not v.usable
    assert len(gw.sent) == 2
    at = v.payload["attempts"]
    assert at[0]["failure_kind"] == "parse"
    assert at[1]["failure_kind"] == "transient"
    assert at[1]["messages_hash"] != at[0]["messages_hash"]  # 修复后的消息
    assert "TRANSPORT_ERROR" in v.basis


# ---------------- 预检拦截：0 次发送 ----------------

def test_context_precheck_zero_sends(tmp_path, monkeypatch):
    class TinyJudge(SemanticJudge):
        def _budget(self, messages):
            return {"fits": False, "reason": "over_window",
                    "estimated_input_tokens": 10 ** 9, "available_tokens": 0}

    gw = FakeGateway([])
    from framework.prompts import PromptTemplate
    tpl = PromptTemplate("judge_eval", "judge-semantic-test", "1", "judge_eval",
                         "JBT_STAGE=JUDGE_EVAL\n{{input_json}}", "")
    j = TinyJudge(gw, tpl, rubric={"success_rule": {}})
    task, cand = _task_candidate()
    v = j.evaluate(task, cand, eval_run_id="e9")
    assert not v.usable and len(gw.sent) == 0
    assert "context_budget_exceeded" in v.basis


def tpl_of(judge):
    return judge.template


# ---------------- 规则版本进协议 ----------------

def test_retry_rule_version_exported():
    assert JUDGE_RETRY_RULE_VERSION == "judge-retry@v3"
