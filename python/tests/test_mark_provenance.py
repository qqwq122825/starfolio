"""Synthetic receipt-time and exchange-isolation regressions; no real markets."""
import json
import sqlite3
import unittest
from test_paper import paper, at, calendar, decision, quote, session


class MarkProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        paper.init_paper(self.conn);paper.import_calendar(self.conn,calendar())
        paper.submit_decision(self.conn,decision())
        self.assertEqual(paper.process_pending(self.conn,'demo',[quote()],session())['filled'],1)
    def tearDown(self):self.conn.close()
    def test_delayed_same_timestamp_holding_mark_cannot_support_earlier_fill(self):
        paper.submit_decision(self.conn,decision(symbol='DEMO02',time='10:02:00'))
        result=paper.process_pending(self.conn,'demo',[
            quote(symbol='DEMO01',time='10:02:01',received_at=at(time='10:02:20')),
            quote(symbol='DEMO02',time='10:02:01')],session(time='10:02:20'))
        self.assertEqual(result['filled'],0)
        self.assertIn('fresh_marks_required_for_all_holdings',str(result))
        self.assertEqual(result['pending'],1)
        # Only after the held-stock information actually exists can a later quote fill.
        result=paper.process_pending(self.conn,'demo',[quote(symbol='DEMO02',time='10:02:21')],session(time='10:02:21'))
        self.assertEqual(result['filled'],1)
        self.assertEqual(result['fills'][0]['filled_at'],paper._stamp(paper._time(at(time='10:02:21'))))
    def test_available_older_mark_can_replace_newer_delayed_packet(self):
        paper.process_pending(self.conn,'demo',[quote(time='10:01:55')],session(time='10:01:55'))
        paper.submit_decision(self.conn,decision(symbol='DEMO02',time='10:02:00'))
        result=paper.process_pending(self.conn,'demo',[
            quote(time='10:02:01',received_at=at(time='10:02:20')),
            quote(symbol='DEMO02',time='10:02:01')],session(time='10:02:20'))
        self.assertEqual(result['filled'],1,result)
    def test_wrong_exchange_quote_cannot_revalue_or_rewrite_holding_mark(self):
        cal=calendar(ident='other-exchange');cal['exchange']='XSHE';paper.import_calendar(self.conn,cal)
        result=paper.process_pending(self.conn,'demo',[quote(time='10:00:02',exchange='XSHE',price=20,bid=19.99,ask=20.01,lower_limit=1,upper_limit=99)],session(time='10:00:02',calendar_id=cal['calendar_id']))
        self.assertIn('quote_exchange_position_mismatch',str(result))
        state=paper.paper_state(self.conn,'demo')
        self.assertEqual(state['positions'][0]['market_value'],999.)
        self.assertEqual(self.conn.execute('SELECT exchange FROM paper_marks').fetchone()[0],'XSHG')
    def test_legacy_wrong_exchange_mark_is_not_a_valuation(self):
        self.conn.execute("UPDATE paper_marks SET exchange='XSHE'")
        state=paper.paper_state(self.conn,'demo')
        self.assertIsNone(state['positions'][0]['market_value']);self.assertFalse(state['equity_is_current'])
        self.assertIsNone(state['equity'])
    def test_late_conflict_invalidates_old_displayed_mark(self):
        paper.process_pending(self.conn,'demo',[quote(quote_id='conflicting-late-copy',bid=9.98)],session())
        state=paper.paper_state(self.conn,'demo')
        self.assertIsNone(state['positions'][0]['market_value']);self.assertFalse(state['equity_is_current'])
        with self.assertRaisesRegex(ValueError,'fresh_marks_required'):
            paper._available_holding_mark(self.conn,'demo','DEMO01','XSHG',paper._time(at(time='10:00:02')),30)
    def test_mark_missing_quote_evidence_not_current(self):
        self.conn.execute('DELETE FROM paper_quotes')
        state=paper.paper_state(self.conn,'demo')
        self.assertIsNone(state['positions'][0]['market_value']);self.assertFalse(state['equity_is_current'])
    def test_future_received_conflict_cannot_increase_buying_power(self):
        for include_conflict in (False,True):
            conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
            try:
                paper.init_paper(conn);paper.import_calendar(conn,calendar())
                paper.submit_decision(conn,decision(quantity=900))
                paper.process_pending(conn,'demo',[quote()],session())
                paper.submit_decision(conn,decision(symbol='DEMO02',quantity=900,time='10:00:02'))
                batch=[quote(time='10:00:03',price=1,bid=.99,ask=1.01,lower_limit=.1,upper_limit=20),
                       quote(symbol='DEMO02',time='10:00:04',price=10.9,bid=10.89,ask=10.91,lower_limit=1,upper_limit=20)]
                if include_conflict:
                    batch.append(quote(time='10:00:03',quote_id='late-conflict',received_at=at(time='10:00:20'),price=1,bid=.98,ask=1.01,lower_limit=.1,upper_limit=20))
                result=paper.process_pending(conn,'demo',batch,session(time='10:00:20'))
                self.assertEqual(result['filled'],0,result)
                self.assertIn('fresh_marks_required' if include_conflict else 'single_security_cap_exceeded',str(result))
            finally:conn.close()
    def test_new_clear_mark_supersedes_older_ambiguous_group(self):
        batch=[quote(time='10:00:02'),quote(time='10:00:02',quote_id='conflict',bid=9.98),quote(time='10:00:03')]
        paper.process_pending(self.conn,'demo',batch,session(time='10:00:03'))
        price=paper._available_holding_mark(self.conn,'demo','DEMO01','XSHG',paper._time(at(time='10:00:04')),30)
        self.assertEqual(float(price),9.99)
    def test_receipt_after_valuation_clock_not_current(self):
        row=self.conn.execute('SELECT quote_key,payload FROM paper_quotes').fetchone()
        raw=json.loads(row['payload']);raw['received_at']=at(time='10:00:20')
        self.conn.execute('UPDATE paper_quotes SET payload=? WHERE quote_key=?',(json.dumps(raw),row['quote_key']))
        self.assertFalse(paper.paper_state(self.conn,'demo')['equity_is_current'])

if __name__=='__main__':unittest.main()
