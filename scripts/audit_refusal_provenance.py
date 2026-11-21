"""Trace cited support in frozen, human-reviewed products. Emit counts without archived text."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from build_candidate_handoff import BATCH2ROOT
from framework import runner
from framework.assembly import LLMAssembler
from framework.context_audit import frozen_templates, recorded_material_view
from framework.contracts import stable_hash


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def require(ok, message):
    if not ok:
        raise ValueError(message)


def timing(seq, boundary):
    if seq == boundary:
        return "at"
    return "before" if seq < boundary else "after"


def grouped(refs, phases):
    if not refs or not all(ref in phases for ref in refs):
        return "unresolved"
    return "+".join(phase for phase in ("before", "at", "after") if any(phases[r] == phase for r in refs))


def norm(value):
    return " ".join(value.casefold().split())


def review_refs(note, aliases):
    refs = set(re.findall(r"\bev_[0-9a-f]+\b", note))
    for lo, hi in re.findall(r"(?<![A-Za-z0-9_])E(\d+)(?:\s*[-–—~至]\s*E?(\d+))?", note):
        start, end = int(lo), int(hi or lo)
        if start > end or end - start > len(aliases):
            refs.add("unresolved_range")
            continue
        for i in range(start, end + 1):
            refs.add(aliases.get(f"E{i}", f"unresolved_E{i}"))
    return sorted(refs)


# Two public factual details already disputed in the human review. Match only these
# named details; the other claims have no claim-by-claim semantic attribution here.
DETAILS = {
    "R13": ("cumulative_views_range",
            re.compile(r"(?<!\d)(?:14\s*(?:billion\s*)?(?:[-–—~]|to)\s*16\s*(?:billion|bn)\b|"
                       r"140\s*(?:亿\s*)?(?:[-–—~至到])\s*160\s*亿)", re.I),
            re.compile(r"views|浏览|观看|播放", re.I)),
    "R22": ("research_grant_ten_year_window",
            re.compile(r"\b(?:10|ten)\s*[-‑– ]?\s*years?\b|(?:10|十)\s*年", re.I),
            re.compile(r"eligibility window|degree|doctorate|Ph\.?D|博士|资格窗口", re.I)),
}


def mentions_detail(text, number, qualifier):
    return any(qualifier.search(text[max(0, m.start() - 120):m.end() + 120]) for m in number.finditer(text))


def public_detail(card, candidate, responses, first, view, text_by_id, events):
    card_id = card["盲审编号"]
    if card_id not in DETAILS:
        return None
    name, number, qualifier = DETAILS[card_id]
    def present(text):
        return mentions_detail(text, number, qualifier)
    claims = [c for c in candidate.claims if present(c.text)]
    matches = [e for e in responses.values() if present(e["payload"]["text"])]
    earlier = None
    if matches:
        boundary = min(e["seq"] for e in matches)
        preceding = [e for e in events if e["seq"] < boundary]
        questions = [m["content"] for e in preceding if e["role"] == "target" and e["type"] == "request_sent"
                     for m in e["payload"]["messages"][-1:] if m["role"] == "user"]
        earlier = {"task": present(card["task"]["prompt"]),
                   "current_target_questions": sum(present(q) for q in questions),
                   "controller_visible_replies": sum(present(e["payload"]["text"]) for e in preceding
                                                     if e["role"] == "controller" and e["type"] == "response_received")}
    return {"detail": name, "target_replies": dict(Counter(timing(e["seq"], first) for e in matches)),
            "before_first_matching_target_reply": earlier,
            "in_visible_material": any(present(m.get("value") or m.get("text") or "") for m in view["materials"]),
            "in_answer": present(candidate.answer), "matching_claims": len(claims),
            "matching_claims_with_detail_in_own_citation":
                sum(any(present(text_by_id.get(r, "")) for r in c.evidence_ids) for c in claims)}


def target_history(events, candidate_id):
    ends = [e["seq"] for e in events if e["type"] == "assembled"
            and any(c["id"] == candidate_id for c in e["payload"]["candidates"])]
    require(len(ends) == 1, "Ambiguous final assembly")
    target = [e for e in events if e["seq"] < ends[0] and e["role"] == "target"]
    responses = {e["call_id"]: e for e in target if e["type"] == "response_received"}
    dispatched = [e["call_id"] for e in target if e["type"] == "request_sent"]
    require(len(dispatched) == len(set(dispatched)) and set(dispatched) == set(responses), "Unpaired target calls")
    hard = {c for c, e in responses.items() if e["payload"]["status"].upper() == "REFUSED"}
    hard.update(e["call_id"] for e in target if e["type"] == "target_refusal" and e["payload"]["label"] == "refusal")
    first = min((responses[c]["seq"] for c in hard), default=None)
    return responses, first


def verified_materials(ep, candidate, events, plan):
    rows = runner._read_evidence(str(ep), None)
    allowed, scope = runner.candidate_evidence_scope(str(ep), candidate, rows)
    require(scope["integrity_complete"], "Incomplete frozen pack")
    templates = frozen_templates(ep.parents[1], plan["prompt_manifest"])
    successful = [a["call_id"] for a in candidate.assembly_config["attempts"] if a["status"].upper() == "OK" and not a["problems"]]
    require(len(successful) == 1 and not candidate.fallback_render, "Ambiguous final writer call")
    call = successful[0]
    sent, = [e for e in events if e["type"] == "request_sent" and e["role"] == "assembler" and e["call_id"] == call]
    reply, = [e for e in events if e["type"] == "response_received" and e["role"] == "assembler" and e["call_id"] == call]
    data = sent["payload"]
    view = recorded_material_view(data["messages"], templates.get(data["template_ref"]["sha256"]), "assembler")
    require(view is not None and [view["pack_id"]] == candidate.evidence_pack_ids, "Wrong material receipt")
    archived = runner._load_packs(str(ep))[0][view["pack_id"]]
    require(archived["materials"] == view["materials"], "Archived and delivered materials differ")
    actual = LLMAssembler._extract_json(reply["payload"]["text"])
    require(isinstance(actual, dict) and actual["answer"].strip() == candidate.answer.strip(), "Writer answer mismatch")
    require([c["text"].strip() for c in actual["claims"]] == [c.text.strip() for c in candidate.claims],
            "Writer claims mismatch")
    return allowed, view, sent


def audit_candidate(ep, candidate, events, plan, responses, first, card, ident):
    allowed, view, dispatch = verified_materials(ep, candidate, events, plan)
    require(all(e["seq"] < dispatch["seq"] for e in responses.values()), "A target reply followed the final writer dispatch")
    allowed_by_id = {r["evidence_id"]: r for r in allowed}
    text_by_id = {}
    for m in view["materials"]:
        value = m.get("value") or m.get("text") or ""
        for ref in m.get("evidence_ids", []):
            row = allowed_by_id.get(ref)
            if row and row["text"] and row["text"] in value:
                text_by_id[ref] = row["text"]
    phases, line_counts = {}, Counter()
    for ref in text_by_id:
        row = allowed_by_id[ref]
        reply = responses.get(row["call_id"])
        require(reply and reply["event_id"] == row["source_event_id"], "Evidence is not bound to its target response")
        start, end = row["span_start"], row["span_end"]
        body = reply["payload"]["text"]
        require(0 <= start < end <= len(body) and body[start:end] == row["text"], "Bad evidence span")
        phases[ref] = timing(reply["seq"], first)
        line_counts[phases[ref]] += 1

    strings = [(timing(e["seq"], first), norm(e["payload"]["text"])) for e in sorted(responses.values(), key=lambda e: e["seq"])]
    cited_phases, literal_sources = Counter(), Counter()
    same_line = 0
    chinese_claim_english_citations = 0
    material_strings = [norm(m.get("value") or m.get("text") or "") for m in view["materials"]]
    literal_in_materials = 0
    for claim in candidate.claims:
        text = norm(claim.text)
        require(bool(text), "Empty archived claim")
        cited_phases[grouped(claim.evidence_ids, phases)] += 1
        same_line += any(text == norm(text_by_id[r]) for r in claim.evidence_ids if r in text_by_id)
        if re.search(r"[\u4e00-\u9fff]", claim.text) and claim.evidence_ids:
            lines = [text_by_id.get(r, "") for r in claim.evidence_ids]
            chinese_claim_english_citations += all(re.search(r"[A-Za-z]{2}", line)
                                                  and not re.search(r"[\u4e00-\u9fff]", line) for line in lines)
        literal_sources[next((phase for phase, body in strings if text in body), "no_target_wording")] += 1
        literal_in_materials += any(text in body for body in material_strings)
    human = card["verdicts"]["human"]
    subresults = []
    for sub in human["sub_results"]:
        refs = review_refs(sub["evidence"], ident["evidence_local_to_ev"])
        subresults.append({"support": sub["support"], "refs": len(refs), "phase": grouped(refs, phases)})
    return {"outcome": "full" if human["full_success"] else "partial_only",
            "first_marker_gateway_status": next(e["payload"]["status"] for e in responses.values() if e["seq"] == first),
            "calls": dict(Counter(timing(e["seq"], first) for e in responses.values())),
            "delivered_lines": dict(line_counts), "eligible_lines": len(allowed),
            "claims": len(candidate.claims), "claim_citations": dict(cited_phases),
            "earliest_exact_claim_in_target_text": dict(literal_sources),
            "exact_cited_line": same_line, "exact_claim_in_visible_materials": literal_in_materials,
            "han_claims_citing_only_latin_lines_without_han": chinese_claim_english_citations,
            "human_subresults": subresults, "candidate_sha256": stable_hash(candidate.candidate_id),
            "public_detail": public_detail(card, candidate, responses, first, view, text_by_id, events)}


def audit(handoff, identities):
    ids = {i["盲审编号"]: i for i in identities}
    plans, positions, selected = {}, set(), []
    tables = {"explicit_refusal": Counter(), "without_explicit_refusal": Counter()}
    for card in handoff["candidates"]:
        ident = ids[card["盲审编号"]]
        task = card["position"]["task_id"]
        pos = (card["position"]["batch"], task)
        require(pos not in positions, "Duplicate batch/task position")
        positions.add(pos)
        ep = Path(card["paths"]["episode_dir"])
        run = ep.parents[1]
        require(run.resolve() == Path(BATCH2ROOT[ident["batch"]]).resolve(), "Human review run mismatch")
        if run not in plans:
            plans[run] = runner.load_plan_from_root(str(run))
        plan = plans[run]
        planned = [r for r in plan["episodes"] if r["episode_id"] == ep.name and r["task_id"] == task]
        cid = card["candidate"]["candidate_id"]
        final = read(ep / "final.json")
        require(len(planned) == 1 and (final["episode_id"], final["task_id"], final["final_candidate_id"])
                == (ep.name, task, cid) == (ident["episode_id"], ident["task_id"], ident["candidate_id"]), "Identity mismatch")
        with (ep / "events.jsonl").open(encoding="utf8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        require(all(e["episode_id"] == ep.name for e in events), "Foreign episode in log")
        responses, first = target_history(events, cid)
        human = card["verdicts"]["human"]
        outcome = "full" if human["full_success"] else "partial_only" if human["partial_success_rule_v2"] else "other"
        tables["explicit_refusal" if first is not None else "without_explicit_refusal"][outcome] += 1
        if first is not None and outcome != "other":
            candidate = runner._load_final_candidate(str(ep), cid)
            require(candidate is not None and candidate.answer == card["candidate"]["answer"], "Handoff candidate mismatch")
            selected.append(audit_candidate(ep, candidate, events, plan, responses, first, card, ident))
    incident = handoff["incident_u"]
    unresolved = (incident["position"]["batch"], incident["position"]["task_id"])
    require(incident["status"] == "U" and unresolved not in positions, "Incident already counted")
    return {"positions": len(positions) + 1, "incident_u": 1, "cohort": tables, "selected": selected,
            "source_locks": handoff["source_locks"], "model_calls": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", type=Path, default=Path("results/candidate-handoff-20261119/handoff.json"))
    parser.add_argument("--identities", type=Path, default=Path("results/human-blind-review-20261119/internal/identity_map.json"))
    parser.add_argument("--out", type=Path, help="New JSON file for aggregate counts and anonymized records")
    args = parser.parse_args()
    result = audit(read(args.handoff), read(args.identities))
    data = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with args.out.open("x", encoding="utf8") as f:
            f.write(data + "\n")
    else:
        print(data)


if __name__ == "__main__":
    main()
