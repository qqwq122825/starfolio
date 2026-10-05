"""Synthetic regressions for a trailing crossing behind a pending stage order."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper
from test_paper import at, calendar, decision, quote, session


class DeferredTrailingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'ledger.db'
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)
        paper.import_calendar(self.conn, calendar())
        paper.submit_decision(self.conn, decision(quantity=600))
        self.assertEqual(paper.process_pending(self.conn, 'demo', [quote()], session())['filled'], 1)
        paper.configure_exit_plan(self.conn, {
            'plan_id': 'deferred-trailing', 'mode': 'demo', 'symbol': 'DEMO01',
            'submitted_at': at(time='10:00:02'), 'source': 'user',
            'reason': 'Synthetic regression only', 'quantity': 600, 'reference_price': 10,
            'stages': [{'gain_pct': .1, 'quantity': 100}, {'gain_pct': .2, 'quantity': 200}],
            'trailing': {'activation_gain_pct': .1, 'distance_pct': .05}, 'test_only': True,
        })

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def restart(self):
        self.conn.commit()
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)

    def state(self):
        return paper.paper_state(self.conn, 'demo')

    def plan(self):
        return self.state()['exit_plans'][0]

    def q(self, bid, time, **changes):
        return quote(day='2026-09-29', time=time, price=bid, bid=bid, ask=bid + .01,
                     lower_limit=1, upper_limit=100, **changes)

    def run_quotes(self, quotes, time):
        return paper.process_pending(self.conn, 'demo', quotes, session(day='2026-09-29', time=time))

    def runq(self, bid, time, **changes):
        return self.run_quotes([self.q(bid, time, **changes)], time)

    def pending(self):
        return [d for d in self.state()['decisions'] if d['status'] == 'pending']

    def latch(self):
        self.runq(11, '10:00:00')
        stage_id = self.plan()['pending_decision_id']
        crossing = self.runq(10, '10:00:01', bid_size=1)
        self.assertEqual(crossing['filled'], 0)
        self.assertEqual(self.plan()['pending_decision_id'], stage_id)
        self.assertEqual(len(self.pending()), 1)
        return stage_id

    def assert_complete(self):
        self.assertEqual(self.plan()['status'], 'completed')
        self.assertEqual(self.plan()['remaining_quantity'], 0)
        self.assertEqual(self.state()['positions'], [])
        self.assertEqual(sum(f['quantity'] for f in self.state()['fills'] if f['side'] == 'sell'), 600)

    def test_crossing_behind_stage_survives_bounce_and_overrides_next_stage(self):
        stage_id = self.latch()
        deferred = self.plan().get('deferred_trailing')
        self.assertIsNotNone(deferred)
        self.assertEqual(deferred['kind'], 'trailing')
        self.assertEqual(deferred['trigger_bid'], '10')
        self.assertNotIn('quantity', deferred)
        self.assertEqual(self.plan()['intent']['kind'], 'stage')
        bounced = self.runq(12, '10:00:02')
        self.assertEqual([f['quantity'] for f in bounced['fills']], [100])
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        self.assertEqual(self.plan()['completed_stages'], [0])
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.assertEqual(self.plan()['intent']['trigger_bid'], '10')
        self.assertEqual(self.plan()['intent']['quantity'], 500)
        self.assertIsNone(self.plan().get('deferred_trailing'))
        self.assertNotEqual(self.plan()['pending_decision_id'], stage_id)
        self.assertEqual([d['quantity'] for d in self.pending()], [500])
        # Promotion and stage fill do not execute the trailing child on the same quote.
        self.assertEqual(self.runq(12, '10:00:02')['filled'], 0)
        self.assertEqual(self.runq(11, '10:00:03')['filled'], 1)
        self.assert_complete()

    def test_first_crossing_is_persistent_and_duplicate_safe_across_restart(self):
        stage_id = self.latch()
        before = self.plan()
        self.restart()
        self.assertEqual(self.plan(), before)
        self.assertEqual(self.runq(10, '10:00:01', bid_size=1)['filled'], 0)
        self.runq(9, '10:00:02', bid_size=1)
        self.runq(12, '10:00:03', bid_size=1)
        self.assertEqual(self.plan().get('deferred_trailing'), before.get('deferred_trailing'))
        self.assertEqual(self.plan()['pending_decision_id'], stage_id)
        self.assertEqual(self.plan()['attempt'], 1)
        deferred_events = [e for e in self.state()['exit_events'] if e['event'] == 'trailing_trigger_deferred']
        self.assertEqual(len(deferred_events), 1)
        self.restart()
        self.runq(12, '10:00:04')
        child_id = self.plan()['pending_decision_id']
        self.restart()
        self.runq(12, '10:00:04')
        self.assertEqual(self.plan()['pending_decision_id'], child_id)
        self.assertEqual(self.plan()['attempt'], 2)
        self.assertEqual(len(self.pending()), 1)
        self.runq(11, '10:00:05')
        self.restart()
        self.runq(11, '10:00:05')
        self.assert_complete()
        self.assertEqual(len([e for e in self.state()['exit_events'] if e['event'] == 'exit_filled']), 2)

    def test_expiry_without_quote_promotes_full_remaining_on_next_fresh_quote(self):
        stage_id = self.latch()
        expired = self.run_quotes([], '10:16:00')
        self.assertEqual(expired['rejected'], 1)
        self.assertIsNone(self.plan()['pending_decision_id'])
        self.assertIsNotNone(self.plan().get('deferred_trailing'))
        self.assertEqual(self.plan()['remaining_quantity'], 600)
        self.restart()
        promoted = self.runq(12, '10:16:01')
        self.assertEqual(promoted['filled'], 0)
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.assertEqual(self.plan()['intent']['quantity'], 600)
        self.assertEqual(self.plan()['completed_stages'], [])
        self.assertNotEqual(self.plan()['pending_decision_id'], stage_id)
        self.assertEqual([d['quantity'] for d in self.pending()], [600])
        self.runq(12, '10:16:02')
        self.assert_complete()

    def test_expiry_on_bounced_quote_creates_only_full_remaining_retry(self):
        self.latch()
        promoted = self.runq(12, '10:16:01')
        self.assertEqual(promoted['rejected'], 1)
        self.assertEqual(promoted['filled'], 0)
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.assertEqual([d['quantity'] for d in self.pending()], [600])
        self.runq(12, '10:16:02')
        self.assert_complete()

    def test_terminal_rejection_preserves_deferred_trailing_and_full_quantity(self):
        self.latch()
        with patch.object(paper, '_execute', side_effect=ValueError('insufficient_cash_for_sell_fees')):
            rejected = self.runq(12, '10:00:02')
        self.assertEqual(rejected['rejected'], 1)
        self.assertEqual(rejected['filled'], 0)
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.assertEqual([d['quantity'] for d in self.pending()], [600])
        self.runq(12, '10:00:03')
        self.assert_complete()

    def test_delayed_trigger_and_stage_fill_receipts_bound_new_child_time(self):
        self.runq(11, '10:00:00')
        quotes = [
            self.q(10, '10:00:01', bid_size=1, received_at=at('2026-09-29', '10:00:04')),
            self.q(12, '10:00:02', received_at=at('2026-09-29', '10:00:05')),
            self.q(12, '10:00:03'),
            self.q(12, '10:00:05'),
        ]
        result = self.run_quotes(quotes, '10:00:05')
        self.assertEqual([f['quantity'] for f in result['fills']], [100])
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        child = self.pending()[0]
        self.assertEqual(child['quantity'], 500)
        expected = paper._time(at('2026-09-29', '10:00:05'))
        self.assertEqual(paper._time(child['submitted_at']), expected)
        self.assertEqual(paper._time(child['evidence_at']), expected)
        self.assertEqual(paper._time(child['exit_context']['information_available_at']), expected)
        self.restart()
        self.assertEqual(self.runq(12, '10:00:05')['filled'], 0)
        self.runq(12, '10:00:06')
        self.assert_complete()

    def test_delayed_high_water_receipt_is_included_in_deferred_evidence(self):
        self.runq(11, '10:00:00')
        quotes = [
            self.q(13, '10:00:01', bid_size=1, received_at=at('2026-09-29', '10:00:05')),
            self.q(12, '10:00:02', bid_size=1),
            self.q(13, '10:00:03'),
            self.q(13, '10:00:04'),
            self.q(13, '10:00:05'),
        ]
        result = self.run_quotes(quotes, '10:00:05')
        self.assertEqual([f['quantity'] for f in result['fills']], [100])
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.assertEqual(paper._time(self.pending()[0]['submitted_at']), paper._time(at('2026-09-29', '10:00:05')))
        self.runq(13, '10:00:06')
        self.assert_complete()

    def test_legacy_state_without_new_optional_fields_can_latch_and_complete(self):
        row = self.conn.execute('SELECT state FROM paper_exit_plans').fetchone()
        state = json.loads(row['state'])
        state.pop('deferred_trailing', None)
        state.pop('information_available_at', None)
        self.conn.execute('UPDATE paper_exit_plans SET state=?', (json.dumps(state),))
        self.restart()
        self.latch()
        self.runq(12, '10:00:02')
        self.assertEqual(self.plan()['intent']['kind'], 'trailing')
        self.runq(12, '10:00:03')
        self.assert_complete()

    def test_cancellation_discards_deferred_intent_without_extra_order(self):
        stage_id = self.latch()
        paper.cancel_exit_plan(self.conn, 'demo', 'deferred-trailing', 'Synthetic cancellation')
        self.restart()
        self.assertEqual(self.runq(12, '10:00:02')['filled'], 0)
        self.assertEqual(self.plan()['status'], 'cancelled')
        self.assertEqual(self.plan()['remaining_quantity'], 600)
        self.assertEqual(self.pending(), [])
        self.assertEqual(next(d['status'] for d in self.state()['decisions'] if d['decision_id'] == stage_id), 'cancelled')
        self.assertEqual(self.plan()['attempt'], 1)

    def test_invalid_crossing_quote_does_not_latch_trailing(self):
        self.runq(11, '10:00:00')
        self.runq(10, '10:00:01', bid_size=1, suspended=True)
        self.assertIsNone(self.plan().get('deferred_trailing'))
        self.runq(11, '10:00:02')
        self.assertIsNone(self.plan()['intent'])
        self.assertEqual(self.plan()['remaining_quantity'], 500)

    def test_late_conflict_quarantines_before_next_child_execution_and_survives_restart(self):
        self.runq(11, '10:00:00')
        child_id = self.plan()['pending_decision_id']
        result = self.run_quotes([
            self.q(10, '10:00:00', quote_id='late-conflicting-signal'),
            self.q(12, '10:00:01'),
        ], '10:00:01')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.plan()['status'], 'active')
        self.assertEqual(self.plan()['remaining_quantity'], 600)
        self.assertEqual(self.plan()['block_reason'], 'ambiguous_prior_quote_requires_cancel')
        self.assertEqual(paper._time(self.plan()['data_conflict_at']), paper._time(at('2026-09-29', '10:00:00')))
        self.assertIsNone(self.plan()['pending_decision_id'])
        self.assertEqual(next(d['status'] for d in self.state()['decisions'] if d['decision_id'] == child_id), 'cancelled')
        self.restart()
        self.runq(12, '10:00:02')
        self.runq(9, '10:00:03')
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.plan()['attempt'], 1)
        self.assertEqual(len([e for e in self.state()['exit_events'] if e['event'] == 'plan_data_quarantined']), 1)
        self.assertEqual(self.state()['data_conflicts'], [{
            'mode': 'demo', 'symbol': 'DEMO01',
            'observed_at': paper._stamp(paper._time(at('2026-09-29', '10:00:00'))),
            'reason': 'late_ambiguous_quote',
        }])
        with self.assertRaisesRegex(ValueError, 'active_exit_plan_requires_cancel'):
            paper.submit_decision(self.conn, decision(side='sell', day='2026-09-29', time='10:00:04'))
        paper.cancel_exit_plan(self.conn, 'demo', 'deferred-trailing', 'Reviewed ambiguous synthetic evidence')
        self.assertEqual(self.plan()['status'], 'cancelled')
        self.assertEqual(paper.submit_decision(self.conn, decision(side='sell', day='2026-09-29', time='10:00:04'))['status'], 'pending')

    def test_late_conflict_keeps_existing_fill_and_cancels_only_pending_child(self):
        self.runq(11, '10:00:00')
        self.runq(12, '10:00:01')
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        child_id = self.plan()['pending_decision_id']
        result = self.run_quotes([
            self.q(11, '10:00:01', quote_id='late-conflicting-stage-fill'),
            self.q(13, '10:00:02'),
        ], '10:00:02')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        self.assertEqual(self.plan()['completed_stages'], [0])
        self.assertEqual(sum(f['quantity'] for f in self.state()['fills'] if f['side'] == 'sell'), 100)
        self.assertEqual(self.state()['positions'][0]['quantity'], 500)
        self.assertEqual(next(d['status'] for d in self.state()['decisions'] if d['decision_id'] == child_id), 'cancelled')

    def test_earlier_ambiguous_quotes_never_consumed_do_not_quarantine_new_plan(self):
        self.run_quotes([
            self.q(11, '10:00:00'),
            self.q(10, '10:00:00', quote_id='originally-ambiguous'),
            self.q(11, '10:00:01'),
        ], '10:00:01')
        self.assertEqual(self.plan()['attempt'], 1)
        self.assertNotIn('_consumed_quote_keys', self.plan())
        self.restart()
        result = self.runq(11, '10:00:02')
        self.assertEqual(result['filled'], 1)
        self.assertIsNone(self.plan().get('data_conflict_at'))
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        self.assertNotIn('data_conflicts', self.state())

    def test_legacy_plan_without_quote_tracker_quarantines_conservatively(self):
        self.runq(11, '10:00:00')
        state = json.loads(self.conn.execute('SELECT state FROM paper_exit_plans').fetchone()['state'])
        state.pop('_consumed_quote_keys')
        self.conn.execute('UPDATE paper_exit_plans SET state=?', (json.dumps(state),))
        self.restart()
        result = self.run_quotes([
            self.q(10, '10:00:00', quote_id='legacy-late-conflict'),
            self.q(12, '10:00:01'),
        ], '10:00:01')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.plan()['block_reason'], 'ambiguous_prior_quote_requires_cancel')
        self.assertEqual(self.pending(), [])

    def test_unrelated_symbol_ambiguity_does_not_quarantine_legacy_plan(self):
        state = json.loads(self.conn.execute('SELECT state FROM paper_exit_plans').fetchone()['state'])
        state.pop('_consumed_quote_keys')
        self.conn.execute('UPDATE paper_exit_plans SET state=?', (json.dumps(state),))
        self.run_quotes([
            self.q(11, '10:00:00', symbol='DEMO02', quote_id='unrelated-high'),
            self.q(10, '10:00:00', symbol='DEMO02', quote_id='unrelated-low'),
            self.q(11, '10:00:01'),
        ], '10:00:01')
        self.restart()
        result = self.runq(11, '10:00:02')
        self.assertEqual(result['filled'], 1)
        self.assertIsNone(self.plan().get('data_conflict_at'))
        self.assertEqual(self.plan()['remaining_quantity'], 500)
        self.assertNotIn('data_conflicts', self.state())

    def test_late_fill_ambiguity_warns_without_any_plan_or_remaining_mark(self):
        self.conn.close()
        self.path = Path(self.temp.name) / 'no-plan-ledger.db'
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)
        paper.import_calendar(self.conn, calendar())
        paper.submit_decision(self.conn, decision(quantity=100))
        paper.process_pending(self.conn, 'demo', [quote()], session())
        before_fills = self.state()['fills']
        self.assertEqual(self.state()['exit_plans'], [])
        # Isolate the durable-fill provenance check from the cached-mark check.
        self.conn.execute('DELETE FROM paper_marks')
        conflicting = quote(quote_id='late-buy-fill-conflict', price=9.8, bid=9.79, ask=9.81)
        result = paper.process_pending(self.conn, 'demo', [conflicting], session())
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.state()['fills'], before_fills)
        self.assertEqual(self.state()['data_conflicts'][0]['reason'], 'late_ambiguous_quote')
        self.assertIn('data_quality_warning', self.state())
        self.assertNotIn('data_conflicts', paper.paper_state(self.conn, 'imported'))
        self.restart()
        paper.process_pending(self.conn, 'demo', [conflicting], session())
        self.assertEqual(len(self.state()['data_conflicts']), 1)
        self.assertEqual(self.state()['fills'], before_fills)

    def test_late_cached_mark_ambiguity_warns_without_active_plan(self):
        paper.cancel_exit_plan(self.conn, 'demo', 'deferred-trailing', 'Synthetic cancellation')
        self.runq(11, '10:00:00')
        self.assertNotIn('data_conflicts', self.state())
        self.runq(10, '10:00:00', quote_id='late-cached-mark-conflict')
        self.assertEqual(len(self.state()['data_conflicts']), 1)
        self.assertEqual(self.state()['data_conflicts'][0]['reason'], 'late_ambiguous_quote')
        self.assertEqual(self.plan()['status'], 'cancelled')
        self.assertIsNone(self.plan().get('data_conflict_at'))
        self.restart()
        self.runq(12, '10:00:01')
        self.assertEqual(len(self.state()['data_conflicts']), 1)
        self.assertIn('data_quality_warning', self.state())


if __name__ == '__main__':
    unittest.main()
