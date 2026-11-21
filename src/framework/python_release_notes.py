"""Versioned CPython documentation and checkpoint semantics for maintenance notes."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path
from uuid import uuid4

from .contracts import stable_hash


ITEMS = ("P1", "P2", "P3")
ANCHORS = ("start", "after_P1", "after_P2", "after_P3")
API = re.compile(r"^( *)(?:\.\. (?:function|method|classmethod|staticmethod)::)\s+([\w.]+)\(")
PUBLIC_NAME = re.compile(r"`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)`")


class InvalidDocumentDecision(ValueError):
    """The reader chose an incompatible action and saved prefix."""


def chapter_sources(fixture, cache: Path, fetch=False):
    sources = {}
    for tag, spec in fixture["releases"].items():
        for module, digest in spec["sha256"].items():
            path = cache / tag / (module + ".rst")
            if fetch and not path.exists():
                url = f"https://raw.githubusercontent.com/python/cpython/{spec['revision']}/Doc/library/{module}.rst"
                with urllib.request.urlopen(url, timeout=30) as response:
                    body = response.read()
                if hashlib.sha256(body).hexdigest() != digest:
                    raise ValueError(f"Downloaded source differs from the frozen revision: {tag}/{module}")
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
                temp.write_bytes(body)
                temp.replace(path)
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError(f"Cached source differs from the frozen revision: {tag}/{module}")
            sources[tag, module] = body.decode("utf-8")
    return sources


def api_entries(text, module, tag, revision):
    """Keep whole Python-domain directives, including nested examples and change notes."""
    lines = text.splitlines()
    entries = []
    for index, line in enumerate(lines):
        match = API.match(line)
        if not match:
            continue
        indentation = len(match[1])
        end = index + 1
        while end < len(lines):
            following = lines[end]
            if following.strip() and len(following) - len(following.lstrip()) <= indentation:
                break
            end += 1
        while end > index + 1 and not lines[end - 1].strip():
            end -= 1
        symbol = module + "." + match[2].lstrip(".")
        entries.append({"kind": "python_api_documentation", "symbol": symbol, "release": tag,
                        "revision": revision, "key": f"Doc/library/{module}.rst",
                        "first_line": index + 1, "last_line": end, "value": "\n".join(lines[index:end]),
                        "origin": "input", "fidelity": "verbatim", "evidence_ids": [f"{tag}-L{index+1}"]})
    return entries


def chapter_window(entries, topic):
    """Full-input baseline around a manifest topic, independent of the question parser."""
    indexes = [i for i, entry in enumerate(entries) if entry["symbol"] == topic]
    if len(indexes) != 1:
        raise ValueError(f"Expected one baseline topic for {topic}, found {len(indexes)}")
    i = indexes[0]
    return entries[max(i - 1, 0):i + 2]


def select_documentation(request, chapters, baseline):
    """Look up the symbols in the public maintenance request in each complete chapter."""
    names = list(dict.fromkeys(PUBLIC_NAME.findall(request)))
    picked = []
    for entries in chapters:
        for name in names:
            matches = [e for e in entries if e["symbol"] == name]
            if len(matches) != 1:
                return baseline, "full_window_unresolved_symbol"
            picked.append(matches[0])
    if not picked:
        return baseline, "full_window_no_symbol"
    return picked, "named_api"


def checkpoint(folder, name, rows):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".json")
    if len(rows) != ANCHORS.index(name):
        raise ValueError("Checkpoint does not contain the named prefix")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(rows, stream, ensure_ascii=False, indent=2)
    return {"name": name, "file": path.name, "sha256": stable_hash(rows)}


def restore(folder, receipt):
    rows = json.loads((folder / receipt["file"]).read_text(encoding="utf-8"))
    if (stable_hash(rows) != receipt["sha256"] or [r["id"] for r in rows] != list(ITEMS[:len(rows)])
            or len(rows) != ANCHORS.index(receipt["name"])):
        raise ValueError("Saved prefix changed")
    return rows


def required_anchor(before, after):
    return next((ANCHORS[i] for i, (x, y) in enumerate(zip(before, after, strict=True)) if x != y), ANCHORS[-1])


def apply_notice(folder, receipts, action, name, reviewed):
    if (action, name) != ("keep", ANCHORS[-1]) and not (action == "revise" and name in ANCHORS[:-1]):
        raise InvalidDocumentDecision("Action and restore point do not agree")
    kept = restore(folder, receipts[name])
    # An unnecessarily late rollback really keeps its stale decisions. Only refresh
    # citations in the retained prefix if the rereading supports the same value.
    rows = list(kept)
    conflicts = []
    for i, original in enumerate(kept):
        if original["value"] == reviewed[i]["value"]:
            rows[i] = reviewed[i]
        else:
            conflicts.append(original["id"])
    rows.extend(reviewed[len(kept):])
    return rows, {"retained": list(ITEMS[:len(kept)]), "rewritten": list(ITEMS[len(kept):]),
                  "retained_disagreements": conflicts}
