"""Synthetic fixtures only. These tests do not contact a market-data service."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import daily_research as d
import investment_watch as w

NOW=datetime.fromisoformat('2026-10-05T12:00:00+08:00')

def row(**kw):
    r=dict(ts_code='600000.SH',trade_date='20260930',open=10.,high=11.,low=9.,close=10.5,pre_close=10.,pct_chg=5.,vol=200.,amount=210.)
    r.update(kw)
    return r

class Provider:
    def __init__(self,rows=None): self.rows=[row()] if rows is None else rows; self.calls=[]
    def fetch(self,*args):self.calls.append(args);return self.rows

class Response(io.BytesIO):
    def geturl(self):return d.ENDPOINT

class Opener:
    def __init__(self,payload):self.payload=payload;self.requests=[]
    def open(self,request,timeout):
        self.requests.append((request,timeout))
        return Response(json.dumps(self.payload).encode())

class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.conn=w.connect(Path(self.tmp.name)/'test.db')
    def tearDown(self):self.conn.close();self.tmp.cleanup()
    def collect(self,rows=None,now=NOW,**kw):return d.collect(self.conn,['600000.SH'],provider=Provider(rows),now=now,**kw)
    def test_october_holiday(self):self.assertEqual(d.expected_session(NOW),date(2026,9,30))
    def test_reopening_before_ingestion(self):self.assertEqual(d.expected_session(datetime.fromisoformat('2026-10-08T16:59:00+08:00')),date(2026,9,30))
    def test_reopening_after_ingestion(self):self.assertEqual(d.expected_session(datetime.fromisoformat('2026-10-08T17:00:00+08:00')),date(2026,10,8))
    def test_makeup_work_saturday_not_trading(self):self.assertFalse(d.is_session(date(2026,10,10)))
    def test_mid_autumn_holiday(self):self.assertFalse(d.is_session(date(2026,9,25)))
    def test_no_calendar_extrapolation(self):
        with self.assertRaisesRegex(d.SourceError,'coverage'):d.expected_session(datetime.fromisoformat('2027-01-05T17:00:00+08:00'))
    def test_naive_clock_rejected(self):
        with self.assertRaises(d.SourceError):d.expected_session(datetime(2026,10,5))
    def test_units_and_provenance(self):
        r=self.collect(); b=r['symbols']['600000.SH']['bars'][0]
        self.assertEqual(r['status'],'ok');self.assertEqual(b['volume_shares'],20000);self.assertEqual(b['turnover_cny'],210000)
        self.assertEqual(b['period_end_at'],'2026-09-30T15:00:00+08:00');self.assertIn('inferred',b['timestamp_basis'])
        self.assertEqual(b['adjustment'],'none');self.assertIsNone(b['large_order_net']);self.assertFalse(b['execution_eligible'])
    def test_no_execution_quotes_or_alerts(self):
        self.collect()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],0)
    def test_stale(self):self.assertEqual(self.collect([row(trade_date='20260929')])['status'],'degraded')
    def test_missing_not_zero_bar(self):
        r=self.collect([]);self.assertEqual(r['symbols']['600000.SH']['status'],'missing');self.assertEqual(r['status'],'degraded')
    def test_stale_state_recomputed(self):
        self.collect();r=d.state(self.conn,datetime.fromisoformat('2026-10-08T17:01:00+08:00'))
        self.assertEqual(r['symbols']['600000.SH']['status'],'stale');self.assertEqual(r['status'],'degraded')
    def test_state_calendar_expiry(self):
        self.collect();r=d.state(self.conn,datetime.fromisoformat('2027-02-01T17:00:00+08:00'))
        self.assertEqual(r['status'],'degraded');self.assertEqual(r['symbols']['600000.SH']['status'],'calendar_unknown')
    def test_failed_run_does_not_show_cached_success(self):
        self.collect()
        with patch.dict(os.environ,{},clear=True):r=d.collect(self.conn,['600000.SH'],now=NOW)
        self.assertEqual(r['status'],'failed');self.assertEqual(d.state(self.conn,NOW)['status'],'failed')
    def test_bad_symbols(self):
        for symbols in [[],['000001.SZ'],['600000.SH']*2,['600000.SH','600001.SH','600002.SH','600003.SH'],['DEMO01']]:
            p=Provider();r=d.collect(self.conn,symbols,provider=p,now=NOW)
            self.assertEqual(r['status'],'failed');self.assertFalse(p.calls)
    def test_bad_lookback(self):
        for days in [0,121,True]:self.assertEqual(self.collect(days=days)['status'],'failed')
    def test_wrong_symbol(self):self.assertEqual(self.collect([row(ts_code='600001.SH')])['status'],'failed')
    def test_duplicate_day(self):self.assertEqual(self.collect([row(),row()])['status'],'failed')
    def test_future_holiday_weekend_rejected(self):
        for day in ['20261008','20261001','20260926']:self.assertEqual(self.collect([row(trade_date=day)])['status'],'failed')
    def test_malformed_date_rejected(self):self.assertEqual(self.collect([row(trade_date='2026-09-30')])['status'],'failed')
    def test_ohlc_consistency(self):self.assertEqual(self.collect([row(high=10)])['status'],'failed')
    def test_nonfinite_negative_and_bool(self):
        for kw in [dict(close=float('nan')),dict(vol=-1),dict(amount=True),dict(pct_chg=float('inf')),dict(open=0)]:
            self.assertEqual(self.collect([row(**kw)])['status'],'failed')
    def test_negative_pct_allowed(self):self.assertEqual(self.collect([row(pct_chg=-5)])['status'],'ok')
    def test_overflow(self):self.assertEqual(self.collect([row(amount=1e308)])['status'],'failed')
    def test_sorted_oldest_first(self):
        r=self.collect([row(),row(trade_date='20260929')]);self.assertEqual(r['symbols']['600000.SH']['bars'][0]['trade_date'],'2026-09-29')
    def test_read_api_state(self):self.assertEqual(w.state(self.conn)['daily_research']['status'],'not_configured')
    def test_https_request_with_no_redirects(self):
        fields=d.FIELDS.split(',');r=row();op=Opener({'code':0,'data':{'fields':fields,'items':[[r[k] for k in fields]]}})
        got=d.TushareDaily(token='synthetic-test-token',opener=op).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
        req,timeout=op.requests[0];self.assertEqual(req.full_url,'https://api.tushare.pro');self.assertEqual(timeout,15);self.assertEqual(got,[r])
    def test_missing_token_no_network(self):
        op=Opener({})
        with self.assertRaisesRegex(d.SourceError,'missing_token'):d.TushareDaily(token='',opener=op).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
        self.assertEqual(op.requests,[])
    def test_provider_message_redacted(self):
        op=Opener({'code':2002,'msg':'secret-token-in-upstream-error'})
        with self.assertRaises(d.SourceError) as cm:d.TushareDaily(token='synthetic',opener=op).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
        self.assertNotIn('secret',str(cm.exception))
    def test_malformed_schema(self):
        for data in [{},{'fields':['ts_code'],'items':[]},{'fields':d.FIELDS.split(','),'items':[[1]]}]:
            with self.assertRaises(d.SourceError):d.TushareDaily(token='synthetic',opener=Opener({'code':0,'data':data})).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
    def test_unhashable_universe_fails_visibly(self):
        for symbols in [[[]],[{}]]:
            self.assertEqual(d.collect(self.conn,symbols,provider=Provider(),now=NOW)['status'],'failed')
    def test_new_year_early_time_out_of_coverage(self):
        with self.assertRaises(d.SourceError):d.expected_session(datetime.fromisoformat('2027-01-01T09:00:00+08:00'))
    def test_missing_expected_date_refreshes(self):
        self.collect([]);r=d.state(self.conn,datetime.fromisoformat('2026-10-08T18:00:00+08:00'))
        self.assertEqual(r['symbols']['600000.SH']['expected_bar_date'],r['expected_bar_date'])
    def test_unhashable_provider_fields(self):
        op=Opener({'code':0,'data':{'fields':[['bad']],'items':[]}})
        with self.assertRaises(d.SourceError):d.TushareDaily(token='synthetic',opener=op).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
    def test_boolean_success_code_rejected(self):
        op=Opener({'code':False,'data':{'fields':d.FIELDS.split(','),'items':[]}})
        with self.assertRaises(d.SourceError):d.TushareDaily(token='synthetic',opener=op).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
    def test_redirect_handler_rejects_even_https(self):
        with self.assertRaisesRegex(d.SourceError,'redirect'):d.NoRedirect().redirect_request(None,None,302,'',{},'https://other.example/')
    def test_changed_response_url_rejected(self):
        class Redirected(Opener):
            def open(self,request,timeout):
                res=Response(b'{}');res.geturl=lambda:'http://api.tushare.pro';return res
        with self.assertRaisesRegex(d.SourceError,'redirect'):d.TushareDaily(token='synthetic',opener=Redirected({})).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
    def test_transport_errors_redacted(self):
        class BadOpener:
            def open(self,*args,**kwargs):raise urllib.error.HTTPError(d.ENDPOINT,403,'secret',None,None)
        with self.assertRaises(d.SourceError) as cm:d.TushareDaily(token='synthetic',opener=BadOpener()).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
        self.assertEqual(str(cm.exception),'https_http_403')
    def test_oversized_and_html_response(self):
        class RawOpener:
            def __init__(self,raw):self.raw=raw
            def open(self,*args,**kwargs):return Response(self.raw)
        for raw in [b'x'*1000001,b'<html>not data</html>']:
            with self.assertRaises(d.SourceError):d.TushareDaily(token='synthetic',opener=RawOpener(raw)).fetch('600000.SH',date(2026,9,1),date(2026,9,30))
    def test_repeated_daily_runs_cannot_trade(self):
        self.collect();self.collect();self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM daily_research_runs').fetchone()[0],2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],0)

if __name__=='__main__':unittest.main()
