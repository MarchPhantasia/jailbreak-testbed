import io
import json
import threading
import time
from pathlib import Path

import pytest

from framework import standalone
from framework.evaluation import REPAIR_INSTRUCTION
from framework.reporting import build_direct3_audit, judge_send_accounting
from test_execution1_r2 import FakeService, routed_cfg
from test_standalone_judge import _write_run_root


SCORED = {"assessment": "scored", "task_fulfillment": "complete", "source_compliance": "met",
          "evidence_sufficiency": "sufficient", "reason": "登记条件有据"}


@pytest.fixture
def run_judge(tmp_path, monkeypatch):
    root = _write_run_root(tmp_path / "archive")
    cfg = routed_cfg(tmp_path)
    monkeypatch.setenv("JBT_UNIT_KEY", "test")
    prompt = Path(__file__).resolve().parents[1] / "prompts/judge_eval.v4.txt"

    def run(**options):
        return standalone.run(cfg, roots=[str(root)], rubric_path=None, prompt_path=str(prompt),
                              out_dir=str(tmp_path / "judge"), **options)
    return run, cfg


class EventStream(io.BytesIO):
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}

    def read1(self, size=-1):
        return super().read1(min(3, size) if size >= 0 else 3)


def stream_response(*, finish=True):
    content = json.dumps(SCORED, ensure_ascii=False)
    def choice(delta, reason=None):
        return {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
    chunks = [choice({"reasoning_content": "私有推理"}),
              choice({"content": content[:15]}), choice({"content": content[15:]}, "stop"),
              {"choices": [], "usage": {"prompt_tokens": 42, "completion_tokens": 11}}]
    frames = [": heartbeat", *["data: " + json.dumps(c, ensure_ascii=False) for c in chunks]]
    if finish:
        frames.append("data: [DONE]")
    return EventStream(("\r\n\r\n".join(frames) + "\r\n\r\n").encode())


def read_events(out):
    path = Path(out["out_dir"]) / "eval/events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_stream_matches_buffered_input_and_requires_complete_verdict(run_judge, monkeypatch):
    run, _ = run_judge
    service = FakeService()
    monkeypatch.setattr("urllib.request.urlopen", service)
    first = run()
    bodies = []

    def stream(request, timeout):
        bodies.append(json.loads(request.data))
        return stream_response(finish=len(bodies) > 1)

    monkeypatch.setattr("urllib.request.urlopen", stream)
    out = run(judge_stream=True, deadline_s=3, resume_from=[first["out_dir"]])
    assert all(b["messages"] == service.bodies[0]["messages"] for b in bodies)
    assert len(bodies) == 2 and all(b["stream"] for b in bodies)
    assert out["rows"][0]["full_success"] and out["rows"][0]["protocol"]["judge_transport"] == "stream"
    attempts = out["rows"][0]["judge_attempts"]
    assert [a["status"] for a in attempts] == ["TRANSPORT_ERROR", "OK"]
    assert attempts[0]["messages_hash"] == attempts[1]["messages_hash"]
    events = read_events(out)
    replies = [e["payload"] for e in events if e["type"] == "response_received"]
    assert replies[0]["status"] == "TRANSPORT_ERROR" and not replies[0]["text"]
    assert replies[1]["reasoning"] == "私有推理" and "私有推理" not in replies[1]["text"]
    assert replies[1]["raw"]["wire_format"] == "sse"
    assert [e["cost"]["tokens_in"] for e in events if e["type"] == "budget_settled"] == [None, 42]
    assert not run(judge_stream=True, resume_from=[out["out_dir"]])["calls_model"]


def test_deadline_settles_once_and_late_reply_cannot_dispatch(run_judge, monkeypatch):
    run, _ = run_judge
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    timeouts = []

    def delayed(request, timeout):
        timeouts.append(timeout)
        started.set()
        release.wait(5)
        finished.set()
        return stream_response()

    monkeypatch.setattr("urllib.request.urlopen", delayed)
    try:
        out = run(judge_stream=True, deadline_s=0.20, timeout_s=30)
        assert started.is_set() and out["rows"][0]["basis"] == "unscored:hard_deadline_exceeded"
        before = read_events(out)
        assert [e["type"] for e in before].count("budget_settled") == 1
        assert out["rows"][0]["judge_attempts"][0]["dispatched"]
        assert next(e["cost"] for e in before if e["type"] == "budget_settled")["tokens_out"] is None
        assert 0 < timeouts[0] <= 0.20
    finally:
        release.set()
    assert finished.wait(2)
    time.sleep(0.03)
    assert len(timeouts) == 1 and read_events(out) == before
    counters = judge_send_accounting(out["out_dir"], "")["per_task"]["t1"]
    assert counters["local_timeout"] == counters["sends"] == 1 and counters["explicit_504"] == 0


def test_standalone_uses_configured_window_before_sending(run_judge, monkeypatch):
    run, cfg = run_judge
    cfg["services"]["general"]["context_window"] = 1024
    fake = FakeService()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    out = run()
    assert out["rows"][0]["basis"] == "unscored:context_budget_exceeded"
    assert not out["calls_model"] and not fake.bodies


def test_audit_matches_interleaved_calls_and_interrupted_runs(tmp_path):
    root = tmp_path / "judge"
    def send(call, task, repair=False):
        msgs = [{"role": "user", "content": "JBT_STAGE=JUDGE_EVAL\n" + json.dumps({
            "evaluation_subject": {}, "task": {"task_id": task}, "evidence": [{"task_id": "wrong"}]})}]
        if repair:
            msgs.extend([{"role": "assistant", "content": "?"}, {"role": "user", "content": REPAIR_INSTRUCTION}])
        return {"type": "request_sent", "role": "judge_eval", "call_id": call, "payload": {"messages": msgs}}

    def reply(call, status, text="", error=""):
        return {"type": "response_received", "role": "judge_eval", "call_id": call,
                "payload": {"status": status, "text": text, "error": error}}

    def write(directory, rid, events):
        archive = directory / "eval"
        archive.mkdir(parents=True)
        (archive / "events.jsonl").write_text(
            "".join(json.dumps(dict(e, episode_id=rid)) + "\n" for e in events) + '{"cut":', encoding="utf-8")

    write(root, "eval_old", [
        send("1", "a"), send("2", "b"), reply("1", "TRANSPORT_ERROR", error="HTTP 504"),
        reply("2", "OK", "bad json"), send("3", "b", repair=True), reply("3", "OK", json.dumps(SCORED)),
        send("4", "a"), send("5", "a"), reply("5", "OK", '{"assessment":"unscored","reason":"unclear"}'),
        send("6", "b"), reply("6", "TRANSPORT_ERROR", error="socket 50401 closed")])
    write(root / "runs/eval_new", "eval_new", [send("1", "b")])
    out = judge_send_accounting(str(root), "eval_new")
    a, b = out["per_task"]["a"], out["per_task"]["b"]
    assert (a["sends"], a["explicit_504"], a["no_receipt"], a["judge_unscored"]) == (3, 1, 1, 1)
    assert (b["sends"], b["valid"], b["other_error"], b["no_receipt"]) == (4, 1, 2, 1)
    assert [d["is_format_repair"] for d in b["send_details"]] == [False, True, False, False]
    assert Path(out["verdicts_path"]).parent == root / "runs/eval_new"
    assert judge_send_accounting(str(root), "")["main_eval_id"] == "eval_old"
    with pytest.raises(ValueError, match="eval_missing"):
        judge_send_accounting(str(root), "eval_missing")


def test_audit_uses_verdict_from_selected_round_even_with_no_sends(tmp_path):
    archive = _write_run_root(tmp_path / "archive")
    plan_file = archive / "plan.json"
    plan = json.loads(plan_file.read_text())
    plan["episodes"][0]["task_id"] = "t1"
    plan_file.write_text(json.dumps(plan))
    (archive / "episodes/ep_1/events.jsonl").touch()
    root = tmp_path / "judge"
    for path, rid in [(root, "eval_first"), (root / "runs/eval_later", "eval_later")]:
        path.mkdir(parents=True)
        row = {"eval_run_id": rid, "task_id": "t1", "episode_id": "ep_1", "candidate_id": "cand_1",
               "verdict_id": rid + "_verdict", "usable": False}
        other = {**row, "candidate_id": "unused", "verdict_id": "wrong"}
        (path / "verdicts.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(other) + "\n")
    audit = build_direct3_audit(str(archive), judge_root=str(root), eval_run_id="eval_later")
    assert audit["main_eval_id"] == "eval_later"
    assert audit["tasks"][0]["judge"]["main_verdict"]["verdict_id"] == "eval_later_verdict"


def test_retry_budget_preserves_first_gateway_error(run_judge, monkeypatch):
    from urllib.error import HTTPError

    run, _ = run_judge
    def timeout(request, timeout):
        raise HTTPError(request.full_url, 504, "Gateway Timeout", None, None)
    monkeypatch.setattr("urllib.request.urlopen", timeout)
    out = run(budget=1)
    row = out["rows"][0]
    assert row["basis"] == "unscored:TRANSPORT_ERROR"
    assert [(a["status"], a["dispatched"]) for a in row["judge_attempts"]] == [
        ("TRANSPORT_ERROR", True), ("BUDGET_EXHAUSTED", False)]
    assert row["judge_attempts"][0]["error"] == "HTTP 504"


def test_reasoning_consumes_entire_output_budget_is_one_send_and_a_specific_U(run_judge, monkeypatch):
    run, _ = run_judge
    def exhausted(request, timeout):
        chunk = {"choices": [{"index": 0, "delta": {"reasoning_content": "分析"},
                              "finish_reason": "length"}],
                 "usage": {"completion_tokens": 16384, "completion_tokens_details": {"reasoning_tokens": 16384}}}
        return EventStream(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
    monkeypatch.setattr("urllib.request.urlopen", exhausted)
    out = run(judge_stream=True, budget=2)
    row = out["rows"][0]
    assert not row["usable"] and row["basis"] == "unscored:reasoning_output_limit"
    attempt, = row["judge_attempts"]
    assert attempt["dispatched"] and attempt["text_chars"] == 0
    assert attempt["output_tokens"] == attempt["reasoning_tokens"] == 16384
    assert attempt["finish_reason"] == "length"
    counts = judge_send_accounting(out["out_dir"], "")["per_task"]["t1"]
    assert counts["sends"] == counts["reasoning_output_limit"] == 1
    assert counts["explicit_504"] == counts["valid"] == 0


@pytest.mark.parametrize("visible", ["", "not JSON"])
def test_judge_format_retry_only_uses_visible_output(visible):
    from test_judge_retry_v2 import FakeCallResult, FakeGateway, _judge, _task_candidate

    gateway = FakeGateway([FakeCallResult("OK", text="<think>scratchpad</think>" + visible),
                           FakeCallResult("OK", text=json.dumps(SCORED))])
    judge = _judge(gateway)
    assert judge.evaluate(*_task_candidate(), eval_run_id="eval_fixture").usable
    first, second = [call["messages"] for call in gateway.sent]
    if visible:
        assert second == first + [{"role": "assistant", "content": visible},
                                  {"role": "user", "content": REPAIR_INSTRUCTION}]
    else:
        assert second == first
