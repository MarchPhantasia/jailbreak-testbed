from dataclasses import asdict
from pathlib import Path

from framework import runner
from framework.contracts import AnswerCandidate, Claim, Fidelity, Origin, SupportStatus


def test_ab_and_batch_preserve_full_candidate_including_fallback(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import assemble_ab

    claim = Claim("c1", "计算出的日期", ["line-2", "line-1"], Origin.DERIVED, SupportStatus.ENTAILED,
                  Fidelity.INFERRED, "assembler")
    model = AnswerCandidate("model", "日期", [claim], ["pack"], ["calendar-rule"], ["待核"],
                            {"arm": "derive", "sample_index": 2}, "stage", ["calc"], False, "done")
    fallback = AnswerCandidate("fallback", "原始记录", evidence_pack_ids=["pack"],
                               assembly_config={"assembler": "material_template", "reason": "format"},
                               fallback_render=True)
    class Batch:
        def candidates(self):
            return [model, fallback]
    batch = tmp_path / "batch.jsonl"
    runner._write_candidates(Batch(), str(batch))
    assemble_ab.persist_arm_candidates(str(tmp_path), [model, fallback])
    assert batch.read_bytes() == (tmp_path / "candidates.jsonl").read_bytes()
    for obj in (model, fallback):
        assert asdict(runner._load_final_candidate(str(tmp_path), obj.candidate_id)) == asdict(obj)
