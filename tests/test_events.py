import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jailbreak_testbed.events import Cost, EventLog, EventType, read_events  # noqa: E402


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seq_is_monotonic_and_survives_reopen(self):
        log = EventLog(self.path)
        e1 = log.append(EventType.PLAN, {"action": "a"})
        e2 = log.append(EventType.RESPONSE, {"response": "r"})
        self.assertEqual((e1.seq, e2.seq), (1, 2))

        log2 = EventLog(self.path)  # 重新打开，seq 应接续
        e3 = log2.append(EventType.TERMINATE, {"reason": "done"})
        self.assertEqual(e3.seq, 3)

    def test_roundtrip_preserves_fields(self):
        log = EventLog(self.path)
        log.append(
            EventType.RESPONSE,
            {"response": "hello"},
            cost=Cost(target_calls=1, tokens_out=12, wall_ms=300),
            served=False,
            budget_after={"target_calls": 19},
            tags={"turn": 2},
        )
        events = read_events(self.path)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev.type, EventType.RESPONSE)
        self.assertEqual(ev.cost.target_calls, 1)
        self.assertFalse(ev.served)
        self.assertEqual(ev.budget_after["target_calls"], 19)
        self.assertEqual(ev.tags["turn"], 2)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(read_events(os.path.join(self.tmp.name, "nope.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
