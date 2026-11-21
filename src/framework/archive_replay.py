"""Read the recorded state and role inputs as of a specific point in a finished trajectory."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import runner
from .assembly import raw_citation_lines, record_model_claim
from .context_audit import frozen_templates, recorded_material_view
from .contracts import stable_hash
from .datasets import sha256_file


DEFAULT_POINTS = ("before-send:1", "after-reply:1", "after-state:1", "before-assembly:1", "end")


def _points(items):
    points = []
    for item in items or DEFAULT_POINTS:
        item = "before-assembly:1" if item == "before-assembly" else item
        if item != "end" and not re.fullmatch(r"(before-send|after-reply|after-state|before-assembly):[1-9][0-9]*", item):
            raise ValueError(f"Unknown replay point: {item}")
        if item not in points:
            points.append(item)
    return points


def _materials(view, watch):
    if not isinstance(view, dict) or not isinstance(view.get("materials"), list):
        return None
    rows = view["materials"]
    ids = {eid for m in rows if isinstance(m, dict) for eid in (m.get("evidence_ids") or []) if isinstance(eid, str)}
    return {"count": len(rows), "distinct_evidence_ids": len(ids),
            "evidence_ids_sha256": stable_hash(sorted(ids)), "watched_ids": sorted(ids & watch)}


def _input_receipt(event, line_no, templates, watch):
    payload = event["payload"]
    messages = payload.get("messages")
    template_sha = (payload.get("template_ref") or {}).get("sha256")
    view = recorded_material_view(messages, templates.get(template_sha), event["role"])
    return {"event_seq": event["seq"], "file_line": line_no, "call_id": event.get("call_id"),
            "messages_sha256": stable_hash(messages) if isinstance(messages, list) else None,
            "materials": _materials(view, watch)}


def _watched_sources(ep_dir, watch):
    by_event = defaultdict(list)
    if watch:
        rows, issues = runner._index_evidence(runner._read_evidence(str(ep_dir), watch))
        conflicts = runner._conflicting_ids(issues)
        for eid, row in rows.items():
            if eid not in conflicts:
                by_event[row.get("source_event_id")].append(row)
    return by_event


def _final_wording(ep_dir, candidate_id):
    if not candidate_id:
        return {"status": "no_final_candidate"}
    candidate = runner._load_final_candidate(str(ep_dir), candidate_id)
    if candidate is None:
        return {"status": "candidate_unavailable", "candidate_id": candidate_id}
    claims = candidate.claims
    info = {"candidate_id": candidate_id, "assembler": candidate.assembly_config.get("assembler"),
            "claim_count": len(claims), "archived_verbatim": sum(c.fidelity == "verbatim" for c in claims),
            "recorded_text_producers": dict(Counter(c.text_producer or "unrecorded" for c in claims))}
    if info["assembler"] != "llm_assembler" or candidate.fallback_render:
        return {**info, "status": "other_producer"}
    packs, issues = runner._load_packs(str(ep_dir))
    conflicted = {r.get("pack_id") for r in issues if r.get("reason") == "conflicting_pack_record"}
    if not candidate.evidence_pack_ids:
        return {**info, "status": "pack_unavailable"}
    materials = []
    for pid in candidate.evidence_pack_ids:
        if pid in conflicted or pid not in packs:
            return {**info, "status": "pack_unavailable"}
        pack = packs[pid]
        rows, errors = runner.verify_pack_row(pack)
        if errors or rows is None:
            return {**info, "status": "pack_unverified"}
        if pack.get("input_hash_v2") and stable_hash({"materials": rows, "components": pack.get("components") or []}) != pack["input_hash_v2"]:
            return {**info, "status": "pack_unverified"}
        materials.extend(rows)
    lines = raw_citation_lines(materials)
    exact = sum(record_model_claim(c.claim_id, c.text, c.evidence_ids, lines).fidelity == "verbatim" for c in claims)
    return {**info, "status": "checked", "exact_cited_raw_line": exact,
            "without_exact_cited_raw_line": len(claims) - exact}


def episode_checkpoints(ep_dir, points, templates, watch):
    """Stream in file order. A checkpoint holds only receipts already present in that prefix."""
    found, latest_inputs, states_by_node, target_calls, reply_turns = {}, {}, {}, {}, {}
    calls = Counter()
    received_ids = set()
    last_state = selected_node = last_pack = chosen = budget = None
    seq = replies = packs = terminations = 0

    def capture(key):
        if key in points and key not in found:
            found[key] = {
                "through_seq": seq, "calls_by_role": dict(calls), "target_replies": replies,
                "last_dispatched_input": dict(latest_inputs), "watched_received_ids": sorted(received_ids),
                "last_state_write": last_state, "selected_node": selected_node,
                "selected_node_state": states_by_node.get(selected_node), "last_pack": last_pack,
                "selected_candidate_id": chosen, "budget": budget,
            }

    archive_status, issue_line = "complete", None
    line_no = 0
    try:
        with (ep_dir / "events.jsonl").open(encoding="utf-8") as stream:
            watched = _watched_sources(ep_dir, watch)
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                ev = json.loads(line)
                if (not isinstance(ev, dict) or ev.get("episode_id") != ep_dir.name
                        or type(ev.get("seq")) is not int or ev["seq"] != seq + 1):
                    raise ValueError("invalid event sequence or identity")
                kind, role, call = ev["type"], ev.get("role", ""), ev.get("call_id")
                payload = ev.get("payload", {})
                if not isinstance(payload, dict):
                    raise ValueError("invalid event payload")
                if kind == "request_sent" and role == "target":
                    capture(f"before-send:{calls['target'] + 1}")
                seq = ev["seq"]
                if ev.get("budget_after"):
                    budget = ev["budget_after"]
                if kind == "request_sent":
                    calls[role] += 1
                    if role == "target" and call:
                        if call in target_calls:
                            raise ValueError("duplicate target dispatch")
                        target_calls[call] = calls[role]
                    if role in ("controller", "assembler"):
                        latest_inputs[role] = _input_receipt(ev, line_no, templates, watch)
                elif kind == "response_received" and role == "target":
                    replies += 1
                    for row in watched.get(ev.get("event_id"), []):
                        text = payload.get("text")
                        start, end = row.get("span_start"), row.get("span_end")
                        if (isinstance(text, str) and type(start) is int and type(end) is int
                                and 0 <= start <= end <= len(text) and text[start:end] == row["text"]):
                            received_ids.add(row["evidence_id"])
                    if call in target_calls:
                        reply_turns[seq] = target_calls[call]
                        capture(f"after-reply:{target_calls[call]}")
                elif kind == "state_write":
                    snap = payload.get("snapshot")
                    node = payload.get("node") or ev.get("node_id")
                    source_seq = (snap or {}).get("source_event_seq", payload.get("state_seq"))
                    last_state = {"event_seq": seq, "file_line": line_no, "node_id": node,
                                  "response_seq": source_seq, "materials": _materials(snap, watch)}
                    states_by_node[node] = last_state
                    if source_seq in reply_turns:
                        capture(f"after-state:{reply_turns[source_seq]}")
                elif kind == "node_selected":
                    selected_node = ev.get("node_id")
                elif kind == "pack_frozen":
                    packs += 1
                    last_pack = {"event_seq": seq, "file_line": line_no, "pack_id": payload.get("pack_id"),
                                 "input_hash": payload.get("input_hash"), "materials": payload.get("materials")}
                    capture(f"before-assembly:{packs}")
                elif kind == "candidate_selected":
                    chosen = payload.get("candidate_id")
                elif kind == "terminate":
                    terminations += 1
                    chosen = payload.get("final_candidate_id")
                    capture("end")
    except FileNotFoundError:
        archive_status = "missing_archive"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        archive_status, issue_line = "unreadable_prefix", line_no
    if terminations > 1:
        archive_status = "multiple_terminations"
    elif archive_status == "complete" and terminations == 0:
        archive_status = "unterminated"
    if archive_status != "complete":
        found.pop("end", None)
    if "end" in found:
        try:
            found["end"]["final_wording"] = _final_wording(ep_dir, found["end"]["selected_candidate_id"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            found["end"]["final_wording"] = {"status": "candidate_unavailable"}
    return {"archive_status": archive_status, "issue_line": issue_line,
            "checkpoints": {point: found.get(point) for point in points}}


def replay_run(root, *, at=None, episodes=None, watch=None):
    root = Path(root)
    plan = runner.load_plan_from_root(str(root))
    points = _points(at)
    entries = plan["episodes"]
    requested = set(episodes or [])
    missing = requested - {e["episode_id"] for e in entries}
    if missing:
        raise ValueError("Unknown episode IDs in the frozen plan: " + ", ".join(sorted(missing)))
    templates = frozen_templates(root, plan.get("prompt_manifest") or [])
    tracked = set(watch or [])
    rows = []
    for entry in entries:
        eid = entry["episode_id"]
        if requested and eid not in requested:
            continue
        # An episode directory is always an immediate child of this run's episodes directory.
        if Path(eid).name != eid or eid in ("", ".", ".."):
            raise ValueError("Invalid episode directory in plan")
        identity = {k: entry.get(k) for k in ("episode_id", "task_id", "exact_task_id", "duplicate_group_id")}
        if not identity["task_id"]:
            identity["task_id"] = (entry.get("task") or {}).get("task_id")
        rows.append({**identity, **episode_checkpoints(root / "episodes" / eid, points, templates, tracked)})
    return {"root": str(root), "points": points, "planned_episodes": len(entries), "selected_episodes": len(rows),
            "available_by_point": {p: sum(r["checkpoints"][p] is not None for r in rows) for p in points},
            "episodes": rows}


def summarize_run(report):
    """Aggregate available prefixes. The summary carries no per-case IDs, paths or message bodies."""
    rows = report["episodes"]
    summary = {
        "plan_sha256": sha256_file(str(Path(report["root"]) / "plan.json")),
        "planned_episodes": report["planned_episodes"], "selected_episodes": len(rows),
        "distinct_exact_tasks": len({r["exact_task_id"] for r in rows if r["exact_task_id"]}),
        "episodes_without_exact_task_id": sum(not r["exact_task_id"] for r in rows),
        "archive_status": dict(Counter(r["archive_status"] for r in rows)), "checkpoints": {},
    }
    for point in report["points"]:
        available = [r["checkpoints"][point] for r in rows if r["checkpoints"][point] is not None]
        dispatches = Counter()
        for checkpoint in available:
            dispatches.update(checkpoint["calls_by_role"])
        summary["checkpoints"][point] = {"available_episodes": len(available), "calls_by_role": dict(dispatches)}
        if point == "end":
            audits = [c["final_wording"] for c in available]
            verified = [a for a in audits if a["status"] == "checked"]
            summary["final_wording"] = {
                "status": dict(Counter(a["status"] for a in audits)),
                "verified_llm_claims": sum(a["claim_count"] for a in verified),
                "archived_verbatim": sum(a["archived_verbatim"] for a in verified),
                "exact_cited_raw_line": sum(a["exact_cited_raw_line"] for a in verified),
            }
    return summary
