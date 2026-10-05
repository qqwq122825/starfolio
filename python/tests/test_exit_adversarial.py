"""Independent, synthetic-only regressions for the optional paper exit engine."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import paper
from test_paper import at, calendar, decision, quote, session


class ExitAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "ledger.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)
        paper.import_calendar(self.conn, calendar())
        paper.submit_decision(self.conn, decision(quantity=300))
        result = paper.process_pending(self.conn, "demo", [quote()], session())
        self.assertEqual(result["filled"], 1)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def restart(self):
        self.conn.commit()
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)

    def plan(self, **changes):
        data = {"plan_id": "adversarial-plan", "mode": "demo", "symbol": "DEMO01",
                "submitted_at": at(time="10:00:02"), "source": "rules", "reason": "Synthetic rule test only",
                "quantity": 100, "reference_price": 10,
                "stages": [{"gain_pct": .01, "quantity": 100}], "test_only": True}
        data.update(changes)
        return paper.configure_exit_plan(self.conn, data)

    def q(self, bid, time, day="2026-09-29", **changes):
        return quote(day=day, time=time, bid=bid, price=bid, ask=bid + .01,
                     lower_limit=1, upper_limit=100, **changes)

    def run_quotes(self, quotes, day="2026-09-29", time="10:00:01"):
        return paper.process_pending(self.conn, "demo", quotes, session(day=day, time=time))

    def state(self):
        return paper.paper_state(self.conn, "demo")

    def current(self):
        return self.state()["exit_plans"][0]

    def test_signal_must_arrive_before_generated_order_can_fill(self):
        self.plan()
        signal = self.q(10.2, "10:00:01", received_at=at("2026-09-29", "10:00:03"))
        earlier = self.q(10.1, "10:00:02")
        result = self.run_quotes([signal, earlier], time="10:00:03")
        self.assertEqual(result["filled"], 0, "Signal is not available until 10:00:03")
        self.assertEqual(self.current()["remaining_quantity"], 100)

    def test_delayed_high_watermark_cannot_drive_earlier_stop_fill(self):
        self.plan(stages=[], trailing={"activation_gain_pct": .01, "distance_pct": .02})
        delayed_high = self.q(10.5, "10:00:01", received_at=at("2026-09-29", "10:00:04"))
        early_drop = self.q(10.0, "10:00:02")
        early_fill = self.q(9.9, "10:00:03")
        result = self.run_quotes([delayed_high, early_drop, early_fill], time="10:00:04")
        self.assertEqual(result["filled"], 0, "High-water evidence was unavailable at the purported fill")

    def test_ambiguous_signal_stays_quarantined_after_subset_replay_and_restart(self):
        self.plan()
        high = self.q(10.2, "10:00:01")
        low = self.q(9.5, "10:00:01", quote_id="conflicting-low")
        first = self.run_quotes([high, low])
        self.assertIn("ambiguous_same_timestamp_quotes", str(first))
        self.restart()
        replay = self.run_quotes([high])
        self.assertEqual(replay["pending"], 0)
        self.assertIsNone(self.current()["high_water_mark"])
        self.assertEqual(self.current()["attempt"], 0)

    def test_ambiguous_execution_stays_quarantined_after_subset_replay(self):
        self.plan()
        self.run_quotes([self.q(10.2, "10:00:01")])
        high = self.q(10.4, "10:00:02")
        low = self.q(9.5, "10:00:02", quote_id="conflicting-execution")
        first = self.run_quotes([high, low], time="10:00:02")
        self.assertEqual(first["filled"], 0)
        replay = self.run_quotes([high], time="10:00:02")
        self.assertEqual(replay["filled"], 0)
        self.assertEqual(self.current()["remaining_quantity"], 100)

    def test_cancellation_event_cannot_predate_plan_creation(self):
        self.plan()
        paper.cancel_exit_plan(self.conn, "demo", "adversarial-plan", "Test cancellation")
        events = {event["event"]: event for event in self.state()["exit_events"]}
        self.assertGreaterEqual(events["plan_cancelled"]["at"], events["plan_created"]["at"])

    def test_stage_signal_never_fills_same_quote_or_same_time(self):
        self.plan()
        first = self.q(10.2, "10:00:01")
        self.assertEqual(self.run_quotes([first])["filled"], 0)
        self.assertEqual(self.current()["remaining_quantity"], 100)
        self.assertEqual(self.run_quotes([first])["filled"], 0)
        equal_time = self.q(10.3, "10:00:01", quote_id="another-id-same-time")
        self.assertEqual(self.run_quotes([equal_time])["filled"], 0)
        self.assertEqual(self.current()["attempt"], 1)

    def test_t_plus_one_intent_survives_lower_next_session_price(self):
        self.plan()
        same_day = self.q(10.2, "10:00:03", day="2026-09-28")
        initial = self.run_quotes([same_day], day="2026-09-28", time="10:00:03")
        self.assertEqual(initial["pending"], 0)
        self.assertIn("t_plus_one", self.current()["block_reason"])
        self.restart()
        next_day = self.run_quotes([self.q(9.5, "10:00:01")])
        self.assertEqual(next_day["filled"], 0)
        self.assertEqual(next_day["pending"], 1)
        final = self.run_quotes([self.q(9.4, "10:00:02")], time="10:00:02")
        self.assertEqual(final["filled"], 1)
        self.assertLess(final["fills"][0]["price"], 9.4)
        self.assertEqual(self.current()["status"], "completed")
        self.assertEqual(self.current()["remaining_quantity"], 0)

    def test_stage_then_trailing_sell_only_plan_remainder_after_restart(self):
        self.plan(quantity=250, stages=[{"gain_pct": .02, "quantity": 100}],
                  trailing={"activation_gain_pct": .03, "distance_pct": .02})
        self.run_quotes([self.q(10.5, "10:00:01")])
        stage = self.run_quotes([self.q(10.6, "10:00:02")], time="10:00:02")
        self.assertEqual(stage["fills"][0]["quantity"], 100)
        self.assertEqual(self.current()["remaining_quantity"], 150)
        self.restart()
        trigger = self.run_quotes([self.q(10.0, "10:00:03")], time="10:00:03")
        self.assertEqual(trigger["filled"], 0)
        trailing = self.run_quotes([self.q(9.5, "10:00:04")], time="10:00:04")
        self.assertEqual(trailing["fills"][0]["quantity"], 150)
        self.assertEqual(self.current()["remaining_quantity"], 0)
        self.assertEqual(self.state()["positions"][0]["quantity"], 50)
        self.restart()
        duplicate = self.run_quotes([self.q(9.5, "10:00:04")], time="10:00:04")
        self.assertEqual(duplicate["filled"], 0)
        self.assertEqual(len([f for f in self.state()["fills"] if f["side"] == "sell"]), 2)
        self.assertEqual(len([e for e in self.state()["exit_events"] if e["event"] == "exit_filled"]), 2)

    def test_expired_child_retries_with_new_later_execution_quote(self):
        self.plan()
        self.run_quotes([self.q(10.2, "10:00:01")])
        old = self.current()["pending_decision_id"]
        self.run_quotes([], time="10:16:02")
        self.assertIsNone(self.current()["pending_decision_id"])
        self.assertEqual(self.current()["remaining_quantity"], 100)
        self.restart()
        retried = self.run_quotes([self.q(9.5, "10:16:03")], time="10:16:03")
        self.assertEqual(retried["filled"], 0)
        self.assertNotEqual(self.current()["pending_decision_id"], old)
        filled = self.run_quotes([self.q(9.4, "10:16:04")], time="10:16:04")
        self.assertEqual(filled["filled"], 1)
        self.assertEqual(self.current()["attempt"], 2)
        self.assertEqual(self.current()["remaining_quantity"], 0)

    def test_cancel_pending_order_prevents_later_fill_and_is_idempotent(self):
        self.plan()
        self.run_quotes([self.q(10.2, "10:00:01")])
        first = paper.cancel_exit_plan(self.conn, "demo", "adversarial-plan", "Test cancel")
        again = paper.cancel_exit_plan(self.conn, "demo", "adversarial-plan", "Repeated cancel")
        self.assertEqual(first, again)
        self.restart()
        result = self.run_quotes([self.q(10.5, "10:00:02")], time="10:00:02")
        self.assertEqual(result["filled"], 0)
        self.assertEqual(self.state()["positions"][0]["quantity"], 300)
        self.assertEqual(self.current()["status"], "cancelled")
        self.assertEqual(self.current()["remaining_quantity"], 100)

    def test_gap_fill_is_actual_next_bid_not_threshold_or_stop(self):
        self.plan(stages=[], trailing={"activation_gain_pct": .01, "distance_pct": .02})
        self.run_quotes([self.q(10.5, "10:00:01")])
        signal = self.run_quotes([self.q(10.0, "10:00:02")], time="10:00:02")
        self.assertEqual(signal["filled"], 0)
        blocked = self.q(9.0, "10:00:03", suspended=True)
        self.assertEqual(self.run_quotes([blocked], time="10:00:03")["filled"], 0)
        final = self.run_quotes([self.q(8.5, "10:00:04")], time="10:00:04")
        self.assertEqual(final["fills"][0]["price"], 8.49)
        self.assertLess(final["fills"][0]["price"], 10.5 * .98)

    def test_receipt_availability_and_strict_later_fill_survive_restart(self):
        self.plan()
        signal = self.q(10.2, "10:00:01", received_at=at("2026-09-29", "10:00:03"))
        self.run_quotes([signal], time="10:00:03")
        pending = self.current()["pending_decision_id"]
        child = next(d for d in self.state()["decisions"] if d["decision_id"] == pending)
        self.assertEqual(paper._time(child["submitted_at"]), paper._time(at("2026-09-29", "10:00:03")))
        self.restart()
        equal_time = self.run_quotes([self.q(10.3, "10:00:03")], time="10:00:03")
        self.assertEqual(equal_time["filled"], 0)
        later = self.run_quotes([self.q(10.1, "10:00:04")], time="10:00:04")
        self.assertEqual(later["filled"], 1)
        self.assertEqual(self.current()["remaining_quantity"], 0)

    def test_generated_order_and_plan_update_roll_back_together_on_failure(self):
        self.plan()
        self.conn.commit()
        before = self.state()
        original = paper.submit_decision

        def simulate_interruption(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("Synthetic interruption after child order insert")

        with patch.object(paper, "submit_decision", side_effect=simulate_interruption):
            with self.assertRaisesRegex(RuntimeError, "Synthetic interruption"):
                self.run_quotes([self.q(10.2, "10:00:01")])
        self.assertEqual(self.state(), before)
        self.restart()
        self.assertEqual(self.state(), before)
        retry = self.run_quotes([self.q(10.2, "10:00:01")])
        self.assertEqual(retry["pending"], 1)
        self.assertEqual(self.current()["attempt"], 1)
        final = self.run_quotes([self.q(10.1, "10:00:02")], time="10:00:02")
        self.assertEqual(final["filled"], 1)
        self.assertEqual(len([f for f in self.state()["fills"] if f["side"] == "sell"]), 1)

    def test_active_plan_blocks_manual_orders_but_allows_hold(self):
        self.plan()
        for side in ("buy", "sell"):
            with self.subTest(side=side), self.assertRaisesRegex(ValueError, "active_exit_plan"):
                paper.submit_decision(self.conn, decision(side=side, time="10:00:03"))
        hold = paper.submit_decision(self.conn, decision(side="hold", time="10:00:03"))
        self.assertEqual(hold["status"], "hold")


if __name__ == "__main__":
    unittest.main()
