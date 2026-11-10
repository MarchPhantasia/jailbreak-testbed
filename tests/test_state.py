import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jailbreak_testbed.state import (  # noqa: E402
    AgentState,
    FailedItem,
    Fact,
    HistoryView,
    StateError,
)


class FactInvariantTest(unittest.TestCase):
    def test_fact_requires_src_events(self):
        with self.assertRaises(StateError):
            Fact(text="位置是 B17", src_events=[])

    def test_invalid_fidelity_rejected(self):
        with self.assertRaises(StateError):
            Fact(text="x", src_events=[1], fidelity="guess")

    def test_confidence_bounds(self):
        with self.assertRaises(StateError):
            Fact(text="x", src_events=[1], confidence=1.5)

    def test_reason_class_is_enum(self):
        with self.assertRaises(StateError):
            FailedItem(attempt="q", reason_class="目标不太愿意")


class AgentStateTest(unittest.TestCase):
    def test_dedup_by_normalized_text(self):
        st = AgentState()
        self.assertTrue(st.add_fact(Fact(text="位置是  B17", src_events=[1])))
        self.assertFalse(st.add_fact(Fact(text="位置是 b17", src_events=[2])))
        self.assertEqual(len(st.facts), 1)

    def test_replace_allowed_explicitly(self):
        st = AgentState()
        st.add_fact(Fact(text="位置是 B17", src_events=[1], fidelity="inferred"))
        st.add_fact(Fact(text="位置是 B17", src_events=[2], fidelity="verbatim"), replace=True)
        self.assertEqual(len(st.facts), 1)
        self.assertEqual(list(st.facts.values())[0].fidelity, "verbatim")

    def test_inferred_ratio_and_warning(self):
        st = AgentState()
        st.add_fact(Fact(text="a", src_events=[1], fidelity="inferred"))
        st.add_fact(Fact(text="b", src_events=[2], fidelity="verbatim"))
        self.assertAlmostEqual(st.inferred_ratio(), 0.5)
        self.assertTrue(st.fidelity_warning(threshold=0.2))
        self.assertFalse(st.fidelity_warning(threshold=0.6))

    def test_payload_roundtrip(self):
        st = AgentState()
        st.add_fact(Fact(text="f1", src_events=[3], confidence=0.4, fidelity="paraphrase"))
        st.add_failed(FailedItem(attempt="q1", reason_class="refusal", src_events=[4]))
        again = AgentState.from_payload(st.to_payload())
        self.assertEqual(again.fact_texts(), ["f1"])
        self.assertEqual(again.failed[0].reason_class, "refusal")


class HistoryViewTest(unittest.TestCase):
    def test_rollback_drops_events_and_bumps_version(self):
        view = HistoryView()
        for seq in (1, 2, 3, 4):
            view.append(seq)
        rolled = view.rollback(drop_from_seq=3)
        self.assertEqual(rolled.visible_seqs, [1, 2])
        self.assertEqual(rolled.view_id, 2)
        # 原视图不被修改
        self.assertEqual(view.visible_seqs, [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()
