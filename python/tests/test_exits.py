"""Synthetic exit-plan acceptance tests. Thresholds do not imply an edge."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper
from test_paper import at, calendar, decision, quote, session


class ExitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        paper.init_paper(self.conn)
        paper.import_calendar(self.conn, calendar())
        paper.submit_decision(self.conn, decision(quantity=600))
        result = paper.process_pending(self.conn, 'demo', [quote()], session())
        self.assertEqual(result['filled'], 1)

    def tearDown(self):
        self.conn.close()

    def data(self, **changes):
        data = {'plan_id': 'test-exit', 'mode': 'demo', 'symbol': 'DEMO01',
                'submitted_at': at(time='10:00:01'), 'source': 'user', 'reason': 'synthetic test only',
                'quantity': 600, 'reference_price': 10, 'stages': [{'gain_pct': .1, 'quantity': 100},
                    {'gain_pct': .2, 'quantity': 200}],
                'trailing': {'activation_gain_pct': .1, 'distance_pct': .05}, 'test_only': True}
        data.update(changes)
        return data

    def plan(self, **changes):
        return paper.configure_exit_plan(self.conn, self.data(**changes))

    def state(self):
        return paper.paper_state(self.conn, 'demo')

    def planstate(self):
        return self.state()['exit_plans'][0]

    def runq(self, value, time, day='2026-09-29', **changes):
        q = quote(day=day, time=time, price=value, bid=value, ask=value+.01, lower_limit=1, upper_limit=100)
        q.update(changes)
        return paper.process_pending(self.conn, 'demo', [q], session(day=day, time=time))

    def test_staged_then_trailing_remainder_and_gap_next_quote(self):
        self.plan()
        result = self.runq(11, '10:00:00')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.assertEqual(self.state()['decisions'][0]['quantity'], 100)
        result = self.runq(12, '10:00:01')
        self.assertEqual(result['fills'][0]['quantity'], 100)
        self.assertEqual(self.planstate()['remaining_quantity'], 500)
        self.assertEqual(self.state()['decisions'][0]['quantity'], 200)
        self.runq(12.5, '10:00:02')
        self.assertEqual(self.planstate()['remaining_quantity'], 300)
        self.assertEqual(self.planstate()['completed_stages'], [0, 1])
        result = self.runq(11.8, '10:00:03')
        self.assertEqual(result['filled'], 0)
        self.assertEqual(self.state()['decisions'][0]['quantity'], 300)
        result = self.runq(10.5, '10:00:04')
        self.assertEqual(result['fills'][0]['quantity'], 300)
        self.assertEqual(result['fills'][0]['price'], 10.49)
        self.assertEqual(self.planstate()['remaining_quantity'], 0)
        self.assertEqual(self.planstate()['status'], 'completed')
        self.assertEqual(self.state()['positions'], [])
        self.assertEqual(sum(f['quantity'] for f in self.state()['fills'] if f['side']=='sell'), 600)
        self.assertEqual(self.plan()['status'], 'completed')

    def test_high_water_never_falls_and_activation_latches(self):
        self.plan(stages=[])
        self.runq(10.5, '10:00:00')
        self.assertFalse(self.planstate()['trailing_active'])
        self.runq(10, '10:00:01')
        self.assertEqual(float(self.planstate()['high_water_mark']), 10.5)
        self.assertIsNone(self.planstate()['pending_decision_id'])
        self.runq(11, '10:00:02')
        self.assertTrue(self.planstate()['trailing_active'])
        self.runq(10.8, '10:00:03')
        self.assertEqual(float(self.planstate()['high_water_mark']), 11)
        self.assertIsNone(self.planstate()['pending_decision_id'])
        self.runq(10.45, '10:00:04')
        self.assertIsNotNone(self.planstate()['pending_decision_id'])

    def test_no_signal_or_fill_at_plan_timestamp(self):
        self.plan(stages=[], trailing={'activation_gain_pct': 0, 'distance_pct': .05})
        self.runq(11, '10:00:01', day='2026-09-28', quote_id='new-same-time')
        self.assertIsNone(self.planstate()['high_water_mark'])
        self.assertIsNone(self.planstate()['pending_decision_id'])

    def test_same_day_trigger_latches_but_t_plus_one_blocks(self):
        self.plan(stages=[{'gain_pct': .1, 'quantity': 600}], trailing=None)
        self.runq(11, '10:00:02', day='2026-09-28')
        self.assertEqual(self.planstate()['block_reason'], 't_plus_one_locked_or_calendar_coverage_missing')
        self.assertIsNone(self.planstate()['pending_decision_id'])
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.runq(10, '10:00:00')
        self.assertIsNotNone(self.planstate()['pending_decision_id'])
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.runq(9.5, '10:00:01')
        self.assertEqual(self.planstate()['remaining_quantity'], 0)

    def test_missing_calendar_stale_and_invalid_quotes_cannot_update_watermark(self):
        self.plan(stages=[])
        result = paper.process_pending(self.conn, 'demo', [quote()], {})
        self.assertEqual(result['status'], 'hold')
        self.assertIsNone(self.planstate()['high_water_mark'])
        for changes in ({'suspended': True}, {'limit_state': 'upper'}, {'source': ''},
                        {'received_at': at('2026-09-29', '10:00:03')}, {'bid': float('nan')}):
            result = self.runq(99, '10:00:02', **changes)
            self.assertEqual(result['status'], 'hold')
            self.assertIsNone(self.planstate()['high_water_mark'])
        result = paper.process_pending(self.conn, 'demo', [quote(day='2026-09-29', time='10:00:03',
            price=20, bid=20, ask=20.01, upper_limit=100)], session(day='2026-09-29', time='10:02:00'))
        self.assertIn('stale_quote', str(result))
        self.assertIsNone(self.planstate()['high_water_mark'])

    def test_limit_and_liquidity_block_fill_without_reducing_remaining(self):
        self.plan(stages=[{'gain_pct': .1, 'quantity': 600}], trailing=None)
        self.runq(11, '10:00:00')
        self.runq(10, '10:00:01', bid_size=100)
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.assertEqual(self.planstate()['block_reason'], 'insufficient_displayed_liquidity')
        self.runq(10, '10:00:02', limit_state='lower')
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.runq(10, '10:00:03')
        self.assertEqual(self.planstate()['remaining_quantity'], 0)

    def test_expiry_retries_only_with_new_decision_and_later_quote(self):
        self.plan(stages=[{'gain_pct': .1, 'quantity': 600}], trailing=None)
        self.runq(11, '10:00:00')
        first = self.planstate()['pending_decision_id']
        paper.process_pending(self.conn, 'demo', [], session(day='2026-09-29', time='10:16:00'))
        self.assertIsNone(self.planstate()['pending_decision_id'])
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.runq(10, '10:16:01')
        self.assertNotEqual(self.planstate()['pending_decision_id'], first)
        self.assertEqual(self.planstate()['remaining_quantity'], 600)
        self.runq(9.5, '10:16:02')
        self.assertEqual(self.planstate()['status'], 'completed')
        self.assertEqual(len([f for f in self.state()['fills'] if f['side']=='sell']), 1)

    def test_idempotency_and_restart(self):
        data = self.data(stages=[])
        self.plan(stages=[])
        self.runq(12, '10:00:00')
        self.runq(11, '10:00:01')
        before = copy.deepcopy(self.state())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger.db'
            disk = sqlite3.connect(path)
            self.conn.commit()
            self.conn.backup(disk)
            disk.close()
            self.conn.close()
            self.conn = sqlite3.connect(path)
            self.conn.row_factory = sqlite3.Row
            paper.init_paper(self.conn)
            self.assertEqual(self.planstate(), before['exit_plans'][0])
            paper.configure_exit_plan(self.conn, data)
            self.runq(11, '10:00:01')
            self.assertEqual(len(self.state()['exit_events']), len(before['exit_events']))
            self.runq(10, '10:00:02')
            self.runq(10, '10:00:02')
            self.assertEqual(len([f for f in self.state()['fills'] if f['side']=='sell']), 1)
            self.assertEqual(self.planstate()['remaining_quantity'], 0)
            with self.assertRaises(ValueError):
                self.runq(11, '10:00:02')

    def test_plan_immutability_rollback_and_mode_isolation(self):
        self.plan()
        self.assertEqual(len(self.state()['exit_plans']), 1)
        with self.assertRaises(ValueError):
            self.plan(reason='changed')
        with self.assertRaises(ValueError):
            self.plan(plan_id='other')
        self.assertEqual(paper.paper_state(self.conn, 'imported')['exit_plans'], [])
        self.assertEqual(paper.paper_state(self.conn, 'imported')['cash'], 100000)
        self.conn.commit()
        before = self.planstate()
        self.runq(11, '10:00:00')
        self.conn.rollback()
        self.assertEqual(self.planstate(), before)

    def test_manual_decisions_must_cancel_plan_first_and_cancel_is_idempotent(self):
        self.plan()
        with self.assertRaises(ValueError):
            paper.submit_decision(self.conn, decision(side='sell', time='10:00:02'))
        paper.submit_decision(self.conn, decision(side='hold', time='10:00:02'))
        self.runq(11, '10:00:00')
        pending = self.planstate()['pending_decision_id']
        result = paper.cancel_exit_plan(self.conn, 'demo', 'test-exit', 'review before changing position')
        self.assertEqual(result['status'], 'cancelled')
        paper.cancel_exit_plan(self.conn, 'demo', 'test-exit', 'same requested cancellation')
        self.runq(11, '10:00:01')
        self.assertEqual(len(self.state()['fills']), 1)
        self.assertEqual(next(d['status'] for d in self.state()['decisions'] if d['decision_id']==pending), 'cancelled')
        paper.submit_decision(self.conn, decision(side='sell', day='2026-09-29', time='10:00:02'))
        self.runq(11, '10:00:03')
        self.assertEqual(self.state()['positions'][0]['quantity'], 500)

    def test_invalid_inputs_fail_closed(self):
        cases = [ {'test_only': False}, {'test_only': 'true'}, {'quantity': True}, {'quantity': 601},
                  {'quantity': -1}, {'reference_price': 0}, {'reference_price': float('inf')},
                  {'source': 'ai_api'}, {'reason': ''}, {'symbol': 'unknown'},
                  {'submitted_at': at(time='10:00:00')}, {'submitted_at': '2999-01-01T00:00:00Z'},
                  {'submitted_at': '2026-09-28T10:00:02'}, {'surprise': 1},
                  {'stages': [], 'trailing': None}, {'stages': 'yes'},
                  {'stages': [{'gain_pct': .1, 'quantity': 600}]},
                  {'stages': [{'gain_pct': .1, 'quantity': 100}], 'trailing': None},
                  {'stages': [{'gain_pct': .2, 'quantity': 100}, {'gain_pct': .1, 'quantity': 100}]},
                  {'stages': [{'gain_pct': .1, 'quantity': False}]},
                  {'stages': [{'gain_pct': .1, 'quantity': 100, 'extra': 1}]},
                  {'trailing': {'activation_gain_pct': -.1, 'distance_pct': .05}},
                  {'trailing': {'activation_gain_pct': .1, 'distance_pct': 1}},
                  {'trailing': {'activation_gain_pct': .1, 'distance_pct': 0}},
                  {'trailing': {'activation_gain_pct': .1}}, ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.plan(**changes)
        self.assertEqual(self.state()['exit_plans'], [])
        self.assertEqual(self.state()['exit_events'], [])

    def test_cannot_attach_before_buy_fills_or_over_pending_manual_order(self):
        with self.assertRaises(ValueError):
            self.plan(symbol='DEMO02')
        paper.submit_decision(self.conn, decision(quantity=100, time='10:00:02'))
        with self.assertRaises(ValueError):
            self.plan(submitted_at=at(time='10:00:02'))

    def test_lower_or_equal_timestamp_cannot_change_high_water(self):
        self.plan(stages=[])
        self.runq(11, '10:00:00')
        self.runq(20, '10:00:00', quote_id='different-id-same-time')
        self.assertEqual(float(self.planstate()['high_water_mark']), 11)
        result = self.runq(20, '09:59:59')
        self.assertIn('simulation_clock_cannot_move_backwards', str(result))
        self.assertEqual(float(self.planstate()['high_water_mark']), 11)

    def test_audit_has_signal_context_and_explicit_limits(self):
        self.plan()
        self.runq(11, '10:00:00')
        state = self.state()
        self.assertTrue(state['decisions'][0]['exit_context']['test_only'])
        self.assertEqual(state['decisions'][0]['source'], 'rules')
        self.assertEqual(state['decisions'][0]['exit_context']['plan_id'], 'test-exit')
        self.assertFalse(state['exit_plans'][0]['validated_edge'])
        self.assertIn('未经收益', state['exit_disclaimer'])
        self.assertIn('exit_triggered', [e['event'] for e in state['exit_events']])
        json.dumps(state, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
