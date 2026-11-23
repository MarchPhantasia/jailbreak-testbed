"""Read-only attribution of Target observations in a single archived episode."""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict


def _classify_nodes(nodes, node_ids, response_calls):
    explicit = defaultdict(list)
    legacy = defaultdict(list)
    parents = {}
    for pos, event in nodes.items():
        data = event.get("payload") or {}
        parent, child = data.get("parent"), event["node_id"]
        if node_ids[child] == 1:
            parents[child] = (pos, parent)
        if not parent:
            continue
        ids = {c for c in (event.get("call_id"), data.get("call_id")) if c}
        response_id = data.get("response_event_id")
        if ids or response_id:
            inferred = response_calls.get(response_id, set())
            ids.update(inferred)
            for c in ids:
                explicit[c].append((pos, len(ids) == 1 and (not response_id or inferred == {c})))
        else:
            legacy[parent].append(pos)
    return explicit, legacy, parents


def _ancestors_observed_before(parent, send_pos, parents, observations):
    inherited, seen = [], set()
    while parent in parents and parent not in seen:
        created, previous = parents[parent]
        if created >= send_pos:
            break
        seen.add(parent)
        if parent in observations:
            inherited.append(observations[parent])
        parent = previous
    return sorted(inherited)


def reconstruct_episode_attribution(events) -> dict:
    """Prefer explicit identities; use event order only for unambiguous legacy calls."""
    sends = []
    parent_sends = defaultdict(list)
    receipts = defaultdict(list)
    refusals = defaultdict(list)
    response_calls = defaultdict(set)
    nodes = {}
    node_ids = Counter()
    for pos, event in enumerate(events):
        kind, call = event.get("type"), event.get("call_id")
        if kind == "request_sent" and event.get("role") == "target":
            sends.append((pos, event))
            parent_sends[event.get("node_id")].append(pos)
        elif kind == "response_received" and event.get("role") in (None, "", "target") and call:
            receipts[call].append((pos, event))
            if event.get("event_id"):
                response_calls[event["event_id"]].add(call)
        elif kind == "target_refusal" and call and event.get("event_id"):
            refusals[call].append((pos, event["event_id"]))
        elif kind == "node_created" and event.get("node_id"):
            nodes[pos] = event
            node_ids[event["node_id"]] += 1

    explicit, legacy, parents = _classify_nodes(nodes, node_ids, response_calls)

    rounds = []
    observations = {}
    last_receipt = {}
    send_counts = Counter(event.get("call_id") for _, event in sends)
    for order, (pos, event) in enumerate(sends, 1):
        call, parent = event.get("call_id"), event.get("node_id")
        matches = receipts.get(call, [])
        unique_call = bool(call) and send_counts[call] == 1
        reply_pos, reply = (None, {})
        if unique_call and len(matches) == 1 and matches[0][0] > pos:
            reply_pos, reply = matches[0]
        selection = None
        if not unique_call:
            reason = "missing_or_reused_call_id"
        elif explicit.get(call):
            links = explicit[call]
            anchor, consistent = links[0]
            node = nodes[anchor]
            valid = (len(links) == 1 and consistent and anchor > pos
                     and node_ids[node["node_id"]] == 1 and node["payload"].get("parent") == parent
                     and (not matches or (reply_pos is not None and anchor > reply_pos)))
            if valid:
                selection, reason = anchor, "explicit"
            else:
                reason = "conflicting_explicit_link"
        elif reply_pos is None:
            reason = "missing_or_ambiguous_receipt"
        else:
            peers = parent_sends[parent]
            next_index = bisect_right(peers, pos)
            end = peers[next_index] if next_index < len(peers) else float("inf")
            possible = legacy[parent]
            choices = possible[bisect_right(possible, reply_pos):bisect_left(possible, end)]
            if last_receipt.get(parent, -1) >= pos or reply_pos >= end:
                reason = "overlapping_requests"
            elif len(choices) == 1 and node_ids[nodes[choices[0]]["node_id"]] == 1:
                selection, reason = choices[0], "chronology"
            else:
                reason = "missing_or_ambiguous_observation"
        for response_pos, _ in matches:
            last_receipt[parent] = max(last_receipt.get(parent, -1), response_pos)

        inherited = _ancestors_observed_before(parent, pos, parents, observations)
        node = nodes.get(selection, {})
        child = node.get("node_id")
        if child:
            observations[child] = order
        payload = reply.get("payload") or {}
        rounds.append({
            "order": order, "call_id": call or "", "send_node": parent or "", "obs_child": child,
            "observation_binding": reason if child else "unresolved", "binding_reason": reason,
            "attribution_rebuilt": reason == "chronology", "inherits_observations": inherited,
            "response_event_id": reply.get("event_id"), "observation_event_id": node.get("event_id"),
            "runtime_label": (node.get("payload") or {}).get("label"),
            "gateway_status": (payload.get("status") or
                               ("unresolved_response" if matches else "no_response_event")),
            "refusal_event_ids": [eid for idx, eid in refusals[call] if unique_call and idx > pos],
            "reply_chars": len(payload.get("text") or ""), "finish_reason": payload.get("finish_reason") or "",
        })
    return {"rounds": rounds, "any_rebuilt": any(r["attribution_rebuilt"] for r in rounds),
            "any_unresolved": any(r["observation_binding"] == "unresolved" for r in rounds)}
