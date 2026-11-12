"""请求产物的权威记录与派生索引（E02）。

契约（contracts/INTERFACES.md「请求产物」）：

- ``request_generated`` 事件是权威记录；``episodes/<id>/requests.jsonl`` 只是可重建索引；
- 只有解析出有效 query 的产物才能发送；无效生成也要留下可复核的失败记录；
- 同一 query 的网络重发保留 request_id、新增 call_id/attempt；新的模型生成或改写
  创建新 request_id 并链接 parent_request_ids；
- Target 的 ``request_sent.payload.request_id`` 必须能反查索引，并且实际发送的
  用户消息与该产物的 query 一致。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .contracts import new_id
from .events import EventStore

STRATEGY_VERSION_DEFAULT = "model_query_v1@1"


@dataclass
class RequestArtifact:
    request_id: str
    task_id: str
    node_id: str
    view_id: str
    query: str
    strategy: str
    strategy_version: str
    parent_request_ids: List[str] = field(default_factory=list)
    generation_call_id: str = ""
    template_ref: Dict[str, Any] = field(default_factory=dict)
    input_evidence_ids: List[str] = field(default_factory=list)
    parse_status: str = "parsed"
    rationale: str = ""
    transform_chain: List[str] = field(default_factory=list)
    generated_event_id: str = ""

    @property
    def valid(self) -> bool:
        """只有解析出非空 query 的产物才允许发送。"""
        return self.parse_status == "parsed" and bool(self.query.strip())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "view_id": self.view_id,
            "query": self.query,
            "strategy": self.strategy,
            "strategy_version": self.strategy_version,
            "parent_request_ids": list(self.parent_request_ids),
            "generation_call_id": self.generation_call_id,
            "template_ref": dict(self.template_ref),
            "input_evidence_ids": list(self.input_evidence_ids),
            "parse_status": self.parse_status,
            "rationale": self.rationale,
            "transform_chain": list(self.transform_chain),
            "generated_event_id": self.generated_event_id,
            "valid": self.valid,
        }


class RequestLedger:
    """把产物写成权威事件，并维护可重建的派生索引。"""

    def __init__(self, store: EventStore, index_path: str):
        self.store = store
        self.index_path = index_path
        self._rows: List[Dict[str, Any]] = []
        self._seq: Dict[str, str] = {}

    # ---- 写 ----
    def record(
        self,
        *,
        task_id: str,
        query: str,
        node_id: str = "",
        view_id: str = "",
        strategy: str = "model_query_v1",
        strategy_version: str = STRATEGY_VERSION_DEFAULT,
        parent_request_ids: Optional[List[str]] = None,
        generation_call_id: str = "",
        template_ref: Optional[Dict[str, Any]] = None,
        input_evidence_ids: Optional[List[str]] = None,
        parse_status: str = "parsed",
        rationale: str = "",
        transform_chain: Optional[List[str]] = None,
        request_id: Optional[str] = None,
    ) -> RequestArtifact:
        artifact = RequestArtifact(
            request_id=request_id or new_id("req"),
            task_id=task_id,
            node_id=node_id,
            view_id=view_id,
            query=query,
            strategy=strategy,
            strategy_version=strategy_version,
            parent_request_ids=list(parent_request_ids or []),
            generation_call_id=generation_call_id,
            template_ref=dict(template_ref or {}),
            input_evidence_ids=list(input_evidence_ids or []),
            parse_status=parse_status,
            rationale=rationale,
            transform_chain=list(transform_chain or []),
        )
        event = self.store.append(
            "request_generated",
            role="controller",
            node_id=node_id,
            view_id=view_id,
            call_id=generation_call_id,
            payload={
                "request_id": artifact.request_id,
                "task_id": task_id,
                "node_id": node_id,
                "view_id": view_id,
                "parent_request_ids": artifact.parent_request_ids,
                "query": query,
                "strategy": strategy,
                "strategy_version": strategy_version,
                "generation_call_id": generation_call_id,
                "template_ref": artifact.template_ref,
                "input_evidence_ids": artifact.input_evidence_ids,
                "parse_status": parse_status,
                "rationale": rationale,
                "transform_chain": artifact.transform_chain,
                "valid": artifact.valid,
                "original_task_frozen": True,
            },
        )
        artifact.generated_event_id = event.event_id
        self._append_index(artifact)
        return artifact

    def record_invalid(
        self,
        *,
        task_id: str,
        node_id: str = "",
        view_id: str = "",
        reason: str = "",
        generation_call_id: str = "",
        template_ref: Optional[Dict[str, Any]] = None,
    ) -> RequestArtifact:
        """无效生成也留档：不发送，但可复核失败原因。"""
        return self.record(
            task_id=task_id,
            query="",
            node_id=node_id,
            view_id=view_id,
            generation_call_id=generation_call_id,
            template_ref=template_ref,
            parse_status=f"invalid:{reason}" if reason else "invalid",
            rationale=reason,
        )

    def _append_index(self, artifact: RequestArtifact) -> None:
        if artifact.request_id in self._seq:  # 一个事件只能有一个索引项
            raise RuntimeError(f"request_id 重复：{artifact.request_id}")
        row = artifact.to_dict()
        self._seq[artifact.request_id] = artifact.generated_event_id
        self._rows.append(row)
        os.makedirs(os.path.dirname(os.path.abspath(self.index_path)), exist_ok=True)
        with open(self.index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---- 读 ----
    def rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    @staticmethod
    def rebuild_from_events(store: EventStore, index_path: str) -> List[Dict[str, Any]]:
        """从事件重建索引（索引可丢弃，事件不可）。"""
        rows: List[Dict[str, Any]] = []
        seen: set = set()
        for ev in store.of_type("request_generated"):
            p = ev.payload or {}
            rid = p.get("request_id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            rows.append(
                {
                    "request_id": rid,
                    "task_id": p.get("task_id", ""),
                    "node_id": p.get("node_id", ""),
                    "view_id": p.get("view_id", ""),
                    "query": p.get("query", ""),
                    "strategy": p.get("strategy", ""),
                    "strategy_version": p.get("strategy_version", ""),
                    "parent_request_ids": p.get("parent_request_ids", []),
                    "generation_call_id": p.get("generation_call_id", ""),
                    "template_ref": p.get("template_ref", {}),
                    "input_evidence_ids": p.get("input_evidence_ids", []),
                    "parse_status": p.get("parse_status", ""),
                    "rationale": p.get("rationale", ""),
                    "transform_chain": p.get("transform_chain", []),
                    "valid": p.get("valid", bool(p.get("query"))),
                    "generated_event_id": ev.event_id,
                }
            )
        with open(index_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return rows


def load_request_index(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row.get("request_id", "")] = row
    return out
