"""I/O helpers for the independent Judge. Only the caller owns the event log and budget."""

import json
import threading
import time


DEFAULT_JUDGE_IDLE_S = 1800.0
JUDGE_STREAM_WAIT_POLICY = "output_progress@1"


class JudgeTimeout(TimeoutError):
    pass


class JudgeWait:
    """One in-flight exchange. Progress extends only its idle timer."""

    def __init__(self, *, idle_s=None, deadline_at=None):
        self.idle_s = idle_s
        self.deadline_at = deadline_at
        self._ready = threading.Condition()
        self._started = self._last = time.monotonic()
        self._first = None
        self._longest_gap = 0.0
        self._updates = 0
        self._outcome = None

    def _remaining(self, now):
        until, reason = self.deadline_at, "hard_deadline_exceeded"
        if self.idle_s is not None:
            idle_until = self._last + self.idle_s
            if until is None or idle_until < until:
                until, reason = idle_until, "judge_idle_timeout"
        if until is None:
            return None
        if until <= now:
            raise JudgeTimeout(reason)
        return until - now

    def remaining(self):
        with self._ready:
            return self._remaining(time.monotonic())

    def socket_timeout(self, configured):
        with self._ready:
            now = time.monotonic()
            self._remaining(now)
            # The socket reuses this interval for subsequent reads. A long
            # connection setup must not shorten every future idle interval.
            if self.deadline_at is not None:
                return min(configured, self.deadline_at - now)
            return configured

    def progress(self):
        with self._ready:
            now = time.monotonic()
            self._remaining(now)  # late bytes cannot revive an expired exchange
            if self._first is None:
                self._first = now
            self._longest_gap = max(self._longest_gap, now - self._last)
            self._last = now
            self._updates += 1
            # Extending the timer needs no wake-up. The owner recomputes it on
            # its previous wake-up time; completion wakes it immediately.

    def summary(self):
        with self._ready:
            return {"idle_timeout_s": self.idle_s, "progress_events": self._updates,
                    "first_progress_ms": None if self._first is None else round((self._first - self._started) * 1000),
                    "last_progress_ms": None if not self._updates else round((self._last - self._started) * 1000),
                    "longest_gap_ms": round(1000 * max(self._longest_gap, time.monotonic() - self._last))}

    def run(self, fn):
        def exchange():
            try:
                self.remaining()
                outcome = (fn(), "")
            except JudgeTimeout as exc:
                outcome = (None, str(exc))
            except Exception as exc:
                outcome = (None, f"call_error:{type(exc).__name__}:{exc}")
            with self._ready:
                try:
                    self._remaining(time.monotonic())
                    self._outcome = outcome
                except JudgeTimeout as exc:
                    self._outcome = (None, str(exc))
                self._ready.notify()

        with self._ready:
            threading.Thread(target=exchange, daemon=True).start()
            while self._outcome is None:
                try:
                    self._ready.wait(self._remaining(time.monotonic()))
                except JudgeTimeout as exc:
                    return None, str(exc)
            return self._outcome


def call_with_deadline(fn, deadline_s):
    if not deadline_s or deadline_s <= 0:
        return fn(), ""
    return JudgeWait(deadline_at=time.monotonic() + deadline_s).run(fn)


def _chunks(response, wait):
    # HTTPResponse.read1 returns on available bytes. A trickle of bytes without
    # a newline must not keep the old exchange alive indefinitely.
    while True:
        wait.remaining()
        chunk = response.read1(65536)
        if not chunk:
            return
        yield chunk


def read_completion_json(response, wait=None):
    if wait is None or not hasattr(response, "read1"):
        return json.loads(response.read().decode("utf-8"))
    body = bytearray()
    for chunk in _chunks(response, wait):
        body.extend(chunk)
        if wait.idle_s is not None and chunk.strip():
            wait.progress()  # service returned a buffered JSON document
    return json.loads(body.decode("utf-8"))


def _lines(response, wait):
    if wait is None or not hasattr(response, "read1"):
        yield from response
        return
    pending = bytearray()
    for chunk in _chunks(response, wait):
        fragments = chunk.split(b"\n")
        for line in fragments[:-1]:
            pending.extend(line)
            yield bytes(pending)
            pending.clear()
        pending.extend(fragments[-1])
    if pending:
        yield bytes(pending)


def _events(response, wait):
    data = []
    for raw in _lines(response, wait):
        if wait is not None:
            wait.remaining()
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def read_judge_stream(response, wait=None):
    """Reassemble choice 0. Only a complete stream can produce a judge verdict."""
    parts = {k: [] for k in ("content", "reasoning_content", "refusal")}
    result = {}
    finish = None
    output_tokens = 0
    for payload in _events(response, wait):
        if payload == "[DONE]":
            if not finish:
                raise ConnectionError("incomplete_judge_stream:missing_finish_reason")
            message = {k: "".join(v) for k, v in parts.items()}
            result["choices"] = [{"message": message, "finish_reason": finish}]
            return result
        chunk = json.loads(payload)
        if not isinstance(chunk, dict):
            raise ValueError("invalid_judge_stream:chunk_not_object")
        if "error" in chunk:
            # An upstream error can arrive inside an HTTP 200 stream.
            error = chunk["error"]
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            raise ConnectionError(f"judge_stream_error:{code[:80]}")
        for k in ("id", "model", "usage"):
            if chunk.get(k) is not None:
                result[k] = chunk[k]
        advanced = False
        usage = chunk.get("usage") or {}
        count = usage.get("completion_tokens", usage.get("output_tokens"))
        if isinstance(count, int) and count > output_tokens:
            output_tokens = count
            advanced = True
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            for k in parts:
                piece = delta.get(k)
                if k == "reasoning_content" and piece is None:
                    piece = delta.get("reasoning")
                if piece is not None:
                    if not isinstance(piece, str):
                        raise ValueError(f"invalid_judge_stream:{k}")
                    if piece:
                        parts[k].append(piece)
                        advanced = True
            if choice.get("finish_reason"):
                advanced = advanced or finish is None
                finish = choice["finish_reason"]
        if advanced and wait is not None:
            wait.progress()
    raise ConnectionError("incomplete_judge_stream:missing_done")
