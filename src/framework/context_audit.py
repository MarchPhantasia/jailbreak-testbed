"""Read the recorded inputs of Controller and Assembler calls after an episode."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .prompts import PLACEHOLDER, PromptRegistry


def message_chars(messages):
    if isinstance(messages, list) and all(isinstance(m, dict) and isinstance(m.get("content"), str) for m in messages):
        return sum(len(m["content"]) for m in messages)
    return None


def _frozen_templates(root, manifest):
    templates = {}
    for entry in manifest:
        if entry.get("role") not in {"controller", "assembler"} or not entry.get("sha256"):
            continue
        try:
            loaded = PromptRegistry.load_frozen(str(root), [entry])
        except (OSError, ValueError):
            continue
        for template in loaded.values():
            templates[template.sha256] = template.text
    return templates


def _input_sections(messages, template, role):
    """Only unpack a recorded message that matches its frozen template on both sides."""
    if not template or len(messages) != 1 or messages[0].get("role") != "user":
        return None
    content = messages[0]["content"]
    before, _, after = template.partition(PLACEHOLDER)
    if not content.startswith(before) or not content.endswith(after):
        return None
    body = content[len(before):len(content) - len(after)]
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    state = data.get("state" if role == "controller" else "pack")
    if not isinstance(state, dict) or not isinstance(state.get("materials"), list):
        return None
    materials = state["materials"]
    values = [m.get("value", m.get("text")) if isinstance(m, dict) else None for m in materials]
    text_chars = sum(map(len, values)) if all(isinstance(v, str) for v in values) else None
    sections = {
        "template_chars": len(before) + len(after),
        "payload_json_chars": len(body),
        "material_count": len(materials),
        "material_text_chars": text_chars,
        "repeated_material_text_chars": text_chars - sum(map(len, set(values))) if text_chars is not None else None,
    }
    if role == "controller":
        queries = state.get("queries")
        sections["branch_query_count"] = len(queries) if isinstance(queries, list) else None
    return sections


def context_profile(events, run_root: Path, manifest=()):
    """Summarize one episode. No role is re-run and no message text is returned."""
    templates = _frozen_templates(run_root, manifest)
    dispatches = Counter()
    settlements = defaultdict(list)
    cache_hits = Counter()
    requests = []
    for event in events:
        kind = event.get("type")
        call_id = event.get("call_id")
        if kind == "budget_settled" and call_id:
            settlements[call_id].append(event.get("cost") or {})
        if kind == "request_sent" and call_id:
            dispatches[call_id] += 1
        role = event.get("role")
        if role not in {"controller", "assembler"}:
            continue
        stage = event.get("stage_id") or None
        if kind == "cache_hit":
            cache_hits[role, stage] += 1
        if kind != "request_sent":
            continue
        payload = event.get("payload") or {}
        messages = payload.get("messages")
        chars = message_chars(messages)
        template_hash = (payload.get("template_ref") or {}).get("sha256")
        requests.append({
            "role": role, "stage_id": stage, "call_id": call_id or None,
            "message_count": len(messages) if isinstance(messages, list) else None,
            "message_chars": chars,
            "sections": _input_sections(messages, templates.get(template_hash), role) if chars is not None else None,
        })

    grouped = defaultdict(list)
    for row in requests:
        cid = row["call_id"]
        costs = settlements.get(cid, [])
        cost = costs[0] if cid and dispatches[cid] == 1 and len(costs) == 1 else {}
        for key in ("tokens_in", "wall_ms"):
            value = cost.get(key)
            row[key] = value if type(value) is int and value >= 0 else None
        grouped[row["role"], row["stage_id"]].append(row)

    stages = []
    for role, stage in sorted(grouped.keys() | cache_hits.keys(), key=lambda k: (k[0], k[1] or "")):
        rows = grouped[role, stage]
        chars = [r["message_chars"] for r in rows if r["message_chars"] is not None]
        tokens = [r["tokens_in"] for r in rows if r["tokens_in"] is not None]
        stages.append({
            "role": role, "stage_id": stage, "dispatched": len(rows), "cache_hits": cache_hits[role, stage],
            "message_chars_known_calls": len(chars), "message_chars_peak": max(chars, default=None),
            "tokens_in_known_calls": len(tokens), "tokens_in_known_sum": sum(tokens) if tokens else None,
            "tokens_in_peak": max(tokens, default=None),
        })
    return {"stages": stages, "requests": requests}
