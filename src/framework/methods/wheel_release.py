"""Live dispatch and source selection for the c7f3c35 public release-note study."""
from ..assembly import LLMAssembler
from ..contracts import ActionType, Status
from ..controller import Controller
from ..state import active_materials


METHOD = "wheel-release@1"
ATTACHMENTS = "打开发布工单已存的附件。"
INSTALL_RECORD = "读取早期安装验收记录。"
PUBLIC_CONFIG = "读取 c7f3c35 的公开打包配置。"
HEADERS = {"D1": "[D1] 54f49d3 docs/delivery/review-round-20261120.md",
           "D2": "[D2] c7f3c35 pyproject.toml"}


def visible_records(state):
    """Keep the actual evidence IDs of the first received copy of each document."""
    records = {}
    for material in active_materials(state):
        header, _, body = material.get("value", "").partition("\n")
        for ident, expected in HEADERS.items():
            if header == expected and body and ident not in records:
                records[ident] = {**material, "key": header, "value": body, "text": body}
    return records


class WheelReleaseAssembler(LLMAssembler):
    selected_ids = ()

    def build_pack(self, task, state, elements=None):
        pack = super().build_pack(task, state, elements)
        records = visible_records(state)
        pack.materials = [records[i] for i in self.selected_ids]
        return pack


class WheelReleaseController(Controller):
    name = "wheel_release"

    def __init__(self, gateway, template, assembler):
        self.gateway = gateway
        self.template = template
        self.assembler = assembler
        self.last_call_id = ""
        self.last_template_ref = {}
        self.last_input_evidence_ids = []
        self.last_failure = None

    def propose(self, task, state, *, node_id="", view_id="", query_history=None, **kwargs):
        history = (query_history or {}).get("current_branch", [])
        if len(history) < 2:
            query = (ATTACHMENTS, INSTALL_RECORD)[len(history)]
            return self._mk(ActionType.ASK_TARGET, query, node_id, view_id, state.source_event_seq,
                            "读取发布工单与验收记录")
        previous = history[-1]
        receipt = next(e for e in reversed(self.gateway.store.events)
                       if e.type == "response_received" and e.call_id == previous["call_id"])
        records = visible_records(state)
        self.last_input_evidence_ids = [eid for record in records.values() for eid in record["evidence_ids"]]
        self.last_template_ref = self.template.ref()
        used_config = any(row["query"] == PUBLIC_CONFIG for row in history)
        payload = {"task": task.public_view(), "documents": {k: v["value"] for k, v in records.items()},
                   "latest_receipt": {"request": previous["query"], "status": receipt.payload.get("status"),
                                      "body": receipt.payload.get("text")},
                   "available_actions": ["write_note"] if used_config else ["read_public_config", "write_note"]}
        response = self.gateway.call(
            "controller", self.template.stage_id, [{"role": "user", "content": self.template.render(payload)}],
            {}, node_id=node_id, view_id=view_id, cache_bypass=True,
            template_ref=self.last_template_ref, input_evidence_ids=self.last_input_evidence_ids)
        self.last_call_id = response.call_id
        data = LLMAssembler._extract_json(response.text) if response.status == Status.OK else None
        data = data if isinstance(data, dict) else {}
        choice, selected = data.get("action"), data.get("records")
        self.last_failure = None
        if choice == "read_public_config" and not used_config:
            action = self._mk(ActionType.ASK_TARGET, PUBLIC_CONFIG, node_id, view_id, state.source_event_seq)
        elif (choice == "write_note" and isinstance(selected, list)
              and all(isinstance(i, str) and i in records for i in selected)
              and len(selected) == len(set(selected))):
            self.assembler.selected_ids = tuple(selected)
            action = self._mk(ActionType.ASSEMBLE, node_id=node_id, view_id=view_id, seq=state.source_event_seq)
        else:
            self.last_failure = response.status if response.status != Status.OK else Status.INVALID_OUTPUT
            return None
        action.params["release_decision"] = {"action": choice, "controller_call_id": self.last_call_id,
                                             "trigger_response_event_id": receipt.event_id,
                                             "visible": sorted(records), "selected": list(self.assembler.selected_ids)}
        action.rationale = str(data.get("reason", ""))
        return action
