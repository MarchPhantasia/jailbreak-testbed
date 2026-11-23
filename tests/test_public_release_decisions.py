import json
from pathlib import Path

import pytest

from framework import runner
from framework.clients import CallResult, MockGateway
from framework.contracts import Status
from framework.methods import independent


@pytest.mark.parametrize("arm,choice,expected", [("available", "all", ["D1", "D2"]),
                                                 ("refused", "all", ["D2"]),
                                                 ("remembered", "all", ["D1", "D2"]),
                                                 ("remembered", "early", ["D1"]),
                                                 ("remembered", "subset", ["D2"])])
def test_public_decision_changes_dispatch_and_frozen_pack(tmp_path, monkeypatch, arm, choice, expected):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import study_live_wheel as study

    def gateway_for(routing, role, guard, store, **kwargs):
        def respond(messages):
            content = messages[-1]["content"]
            obj = json.loads(content[content.index("{\n"):])
            if role == "controller":
                read = "read_public_config" in obj["available_actions"] and choice != "early"
                selected = ["D2"] if not read and choice == "subset" else sorted(obj["documents"])
                data = {"action": "read_public_config" if read else "write_note", "records": selected}
            else:
                ids = obj["pack"]["evidence_ids"]
                answer = "P1=U P2=N P3=Y\n归档说明。"
                data = {"answer": answer, "claims": [{"claim_id": "c1", "text": "归档说明。", "evidence_ids": ids}],
                        "gaps": []}
            return CallResult("", Status.OK, text=json.dumps(data, ensure_ascii=False))
        return MockGateway(guard, store, scripts={"*": [respond]})

    monkeypatch.setattr(independent, "build_gateway", gateway_for)
    svc = {"type": "mock", "model": "local", "parameter_profile": "qwen38_vllm"}
    cfg = runner.load_config(str(study.prepare_arm(tmp_path, arm, svc, study.read(study.FIXTURE))))
    runner.run_experiment(cfg)
    output = study.inspect_arm(tmp_path / arm, study.CONDITIONS[arm])
    assert output["write_confirmed"] and output["documents"] == expected
    assert output["citations_in_pack"]
    assert output["passed"] is (arm == "refused")
    assert output["config_read_after_receipt"] is (choice != "early")
    assert output["calls"]["target"] == (2 if choice == "early" else 3)
    assert output["decisions"][0]["trigger_response_event_id"] == output["response_id"]
