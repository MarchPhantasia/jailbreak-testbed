"""I/O helpers for the independent Judge. Only the caller owns the event log and budget."""

import json
import queue
import threading
import time


def call_with_deadline(fn, deadline_s):
    if not deadline_s or deadline_s <= 0:
        return fn(), ""
    box = queue.Queue(maxsize=1)

    def exchange():
        try:
            box.put((fn(), ""))
        except Exception as exc:
            box.put((None, f"call_error:{type(exc).__name__}:{exc}"))

    until = time.monotonic() + deadline_s
    threading.Thread(target=exchange, daemon=True).start()
    try:
        return box.get(timeout=max(0, until - time.monotonic()))
    except queue.Empty:
        return None, "hard_deadline_exceeded"


def _chunks(response, deadline_at):
    # HTTPResponse.read1 returns on available bytes. A trickle of bytes without
    # a newline must not keep the old exchange alive indefinitely.
    while time.monotonic() < deadline_at:
        chunk = response.read1(65536)
        if not chunk:
            return
        yield chunk
    raise TimeoutError("hard_deadline_exceeded")


def read_completion_json(response, deadline_at=None):
    if deadline_at is None or not hasattr(response, "read1"):
        return json.loads(response.read().decode("utf-8"))
    return json.loads(b"".join(_chunks(response, deadline_at)).decode("utf-8"))


def _lines(response, deadline_at):
    if deadline_at is None or not hasattr(response, "read1"):
        yield from response
        return
    pending = bytearray()
    for chunk in _chunks(response, deadline_at):
        fragments = chunk.split(b"\n")
        for line in fragments[:-1]:
            pending.extend(line)
            yield bytes(pending)
            pending.clear()
        pending.extend(fragments[-1])
    if pending:
        yield bytes(pending)


def _events(response, deadline_at):
    data = []
    for raw in _lines(response, deadline_at):
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TimeoutError("hard_deadline_exceeded")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def read_judge_stream(response, deadline_at=None):
    """Reassemble choice 0. Only a complete stream can produce a judge verdict."""
    parts = {k: [] for k in ("content", "reasoning_content", "refusal")}
    result = {}
    finish = None
    for payload in _events(response, deadline_at):
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
                    parts[k].append(piece)
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    raise ConnectionError("incomplete_judge_stream:missing_done")
