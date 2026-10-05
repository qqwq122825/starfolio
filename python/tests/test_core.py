import sys, tempfile, unittest, json, threading, urllib.request, urllib.error
from pathlib import Path
from datetime import timedelta
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import investment_watch as w

class CoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.conn=w.connect(Path(self.temp.name)/'db.sqlite')
 def tearDown(self):
  self.conn.close();self.temp.cleanup()
 def real(self,**kw):
  s=w.demo_data()[0];s.update(symbol='600000',mode='imported',source='测试来源，不是真实市场',source_url='https://www.sse.com.cn/');s.update(kw);return s
 def watch_real(self):
  self.conn.execute('INSERT INTO watchlist VALUES(?,?,1,?)',('600000','测试',w.stamp()));self.conn.commit()
 def test_demo_is_explicit_and_segregated(self):
  r=w.run_monitor(self.conn,'demo');self.assertEqual(r['status'],'ok');self.assertEqual(r['new_alerts'],2)
  self.assertTrue(all(s['symbol'].startswith('DEMO') for s in w.latest_snapshots(self.conn,'demo').values()))
  self.assertEqual(w.latest_snapshots(self.conn,'imported'),{})
 def test_repeated_run_deduplicates(self):
  w.run_monitor(self.conn,'demo');r=w.run_monitor(self.conn,'demo');self.assertEqual(r['new_alerts'],0)
  self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],2)
 def test_reviewed_state_survives_scan(self):
  w.run_monitor(self.conn,'demo');self.conn.execute("UPDATE alerts SET status='reviewed'");self.conn.commit();w.run_monitor(self.conn,'demo')
  self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE status='reviewed'").fetchone()[0],2)
 def test_stale_blocks_anomaly(self):
  self.assertIsNone(w.anomaly(self.real(observed_at=w.stamp(w.utcnow()-timedelta(minutes=21)))))
 def test_failed_provider_retries_without_demo_fallback(self):
  count=[]
  def fail():count.append(1);raise OSError('数据源不可用')
  result=w.run_monitor(self.conn,'live',provider=fail,backoff=0)
  self.assertEqual(len(count),3);self.assertEqual(result['status'],'failed')
  self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],0)
  self.assertEqual(w.setting(self.conn,'mode'),'live')
 def test_transient_retry_succeeds(self):
  self.watch_real();calls=[]
  def source():
   calls.append(1)
   if len(calls)<2:raise TimeoutError('暂时失败')
   return [self.real()]
  self.assertEqual(w.run_monitor(self.conn,'imported',provider=source,backoff=0)['status'],'ok')
  self.assertEqual(len(calls),2)
 def test_live_unconfigured_fails_closed(self):
  result=w.run_monitor(self.conn,'live');self.assertEqual(result['status'],'failed');self.assertIn('未自动回退',result['detail'])
 def test_stale_import_run_degraded(self):
  self.watch_real();s=self.real(observed_at=w.stamp(w.utcnow()-timedelta(days=5)))
  w.import_market(self.conn,[s]);r=w.run_monitor(self.conn,'imported');self.assertEqual(r['status'],'degraded');self.assertEqual(r['new_alerts'],0)
 def test_insufficient_baseline_no_signal(self):self.assertIsNone(w.anomaly(self.real(baseline=[1e8]*3)))
 def test_constant_baseline_no_signal(self):self.assertIsNone(w.anomaly(self.real(baseline=[1e8]*20)))
 def test_no_flow_not_invented(self):self.assertIsNone(w.anomaly(self.real(large_order_net=None)))
 def test_future_rejected(self):
  with self.assertRaises(ValueError):w.validate_snapshot(self.real(observed_at=w.stamp(w.utcnow()+timedelta(hours=1))))
 def test_naive_time_rejected(self):
  with self.assertRaises(ValueError):w.validate_snapshot(self.real(observed_at='2026-10-05T08:00:00'))
 def test_nonfinite_rejected(self):
  with self.assertRaises(ValueError):w.validate_snapshot(self.real(price=float('nan')))
 def test_missing_source_and_unit_errors(self):
  for kw in [{'source':''},{'flow_definition':''},{'large_order_net':1e12}]:
   with self.assertRaises(ValueError):w.validate_snapshot(self.real(**kw))
 def test_source_url_blocks_js_and_credentials(self):
  for url in ['javascript:alert(1)','https://name:secret@example.com','http://example.com']:
   with self.assertRaises(ValueError):w.validate_snapshot(self.real(source_url=url))
 def test_import_batch_atomic(self):
  with self.assertRaises(ValueError):w.import_market(self.conn,[self.real(),self.real(price=-1)])
  self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0],0)
 def test_portfolio_csv_roundtrip_and_atomic(self):
  w.import_portfolio(self.conn,'symbol,name,shares,cost\n600000,测试,100,10\n')
  for text in ['symbol,name,shares,cost\n600000,测试,-1,10\n','symbol,name,shares,cost\n600000,测试,100,NaN\n','symbol,name,shares,cost,account\n600000,测试,100,10,私密\n']:
   with self.assertRaises(ValueError):w.import_portfolio(self.conn,text)
  self.assertEqual(self.conn.execute('SELECT shares FROM portfolio').fetchone()[0],100)
 def test_unverified_news_never_promotes_alert(self):
  n={'symbol':'600000','title':'测试订单','source_url':'https://www.sse.com.cn/','stage':'order','verification':'unverified','evidence_excerpt':'测试原文，未经核验'}
  w.add_news(self.conn,n);self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],0)
 def test_manual_review_requires_evidence_and_reviewer(self):
  with self.assertRaises(ValueError):w.add_news(self.conn,{'symbol':'600000','title':'测试','source_url':'https://www.sse.com.cn/','verification':'reviewed','stage':'order'})
 def test_reviewed_order_promotes_dedup_alert(self):
  n={'symbol':'600000','title':'测试订单','source_url':'https://www.sse.com.cn/','stage':'order','verification':'reviewed','reviewed_by':'测试人员','evidence_excerpt':'仅为测试用数据'}
  w.add_news(self.conn,n);w.add_news(self.conn,n);self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],1)
 def test_old_order_no_new_alert(self):
  w.add_news(self.conn,{'symbol':'600000','title':'历史订单','published_at':w.stamp(w.utcnow()-timedelta(days=4)),'source_url':'https://www.sse.com.cn/','stage':'order','verification':'reviewed','reviewed_by':'测试人员','evidence_excerpt':'历史证据'})
  self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],0)
 def test_digest_discloses_failures(self):
  w.run_monitor(self.conn,'live');d=w.digest(self.conn,'live');self.assertIn('failed',d);self.assertIn('不连接券商',d)
 def test_persistence_reopen(self):
  w.run_monitor(self.conn,'demo');c=w.connect(Path(self.temp.name)/'db.sqlite');self.assertEqual(c.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],2);c.close()

class HTTPTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.server=w.Server(('127.0.0.1',0),str(Path(cls.temp.name)/'db.sqlite'));cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.base=f'http://127.0.0.1:{cls.server.server_port}'
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.temp.cleanup()
 def post(self,path,data,token=True,extra=None):
  headers={'Content-Type':'application/json'}
  if token:headers['X-Watch-Token']=self.server.token
  headers.update(extra or {})
  return urllib.request.urlopen(urllib.request.Request(self.base+path,data=json.dumps(data).encode(),headers=headers),timeout=4)
 def test_http_read_and_mutation(self):
  state=json.load(urllib.request.urlopen(self.base+'/api/state'));self.assertEqual(state['mode'],'demo')
  self.assertTrue(json.load(self.post('/api/watchlist',{'symbol':'600000','name':'测试'}))['saved'])
 def test_csrf_rejected(self):
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.post('/api/run',{'mode':'demo'},token=False)
  self.assertEqual(ctx.exception.code,403)
 def test_cross_origin_rejected(self):
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.post('/api/run',{'mode':'demo'},extra={'Origin':'https://attacker.invalid'})
  self.assertEqual(ctx.exception.code,403)
 def test_trade_endpoint_absent(self):
  for path in ['/api/trade','/api/order','/api/broker','/api/buy','/api/sell']:
   with self.assertRaises(urllib.error.HTTPError) as ctx:self.post(path,{})
   self.assertEqual(ctx.exception.code,404)
 def test_paper_http_calendar_decision_fill_and_dedup(self):
  root=Path(__file__).resolve().parents[1]/'examples'
  cal=json.loads((root/'paper-demo-calendar.json').read_text())
  decision=json.loads((root/'paper-demo-decision.json').read_text())
  process=json.loads((root/'paper-demo-process.json').read_text())
  self.assertEqual(json.load(self.post('/api/paper/calendar',cal))['mode'],'demo')
  self.assertEqual(json.load(self.post('/api/paper/decision',decision))['status'],'pending')
  self.assertEqual(json.load(self.post('/api/paper/process',process))['filled'],1)
  self.assertEqual(json.load(self.post('/api/paper/process',process))['filled'],0)
  state=json.load(urllib.request.urlopen(self.base+'/api/state'))
  self.assertEqual(state['paper']['cash'],98992.99)
 def test_paper_exit_routes_and_csrf(self):
  plan={'mode':'demo','plan_id':'http-test-plan','test_only':True}
  with mock.patch.object(w.paper,'configure_exit_plan',return_value={'plan_id':'http-test-plan'},create=True) as call:
   self.assertEqual(json.load(self.post('/api/paper/exit-plan',plan))['plan_id'],'http-test-plan')
   self.assertEqual(call.call_args.args[1],plan)
  cancellation={'mode':'demo','plan_id':'http-test-plan','reason':'测试取消'}
  with mock.patch.object(w.paper,'cancel_exit_plan',return_value={'status':'cancelled'},create=True) as call:
   self.assertEqual(json.load(self.post('/api/paper/exit-cancel',cancellation))['status'],'cancelled')
   self.assertEqual(call.call_args.args[1:],('demo','http-test-plan','测试取消'))
  for path in ['/api/paper/exit-plan','/api/paper/exit-cancel']:
   with self.assertRaises(urllib.error.HTTPError) as ctx:self.post(path,{},token=False)
   self.assertEqual(ctx.exception.code,403)
 def test_http_news_requires_separate_evidence_and_returns_hold(self):
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.post('/api/news',{'symbol':'600000','title':'仅测试价格','source_url':'https://www.sse.com.cn/','stage':'order','price_reaction':'up','price_reaction_evidence':'只有价格上涨'})
  self.assertEqual(ctx.exception.code,400)
  n=json.load(self.post('/api/news',{'symbol':'600000','title':'未知研究状态','source_url':'https://www.sse.com.cn/'}))['news']
  self.assertEqual(n['research_assessment']['action'],'HOLD')
  self.assertEqual(n['expectation_status'],'unknown')
 def test_exit_http_real_engine_configure_trigger_fill_and_cancel(self):
  from test_paper import at, calendar, decision, quote, session
  # Isolated server/database so clock progression does not alter other HTTP cases.
  with tempfile.TemporaryDirectory() as tmp:
   server=w.Server(('127.0.0.1',0),str(Path(tmp)/'exit.sqlite'))
   worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
   base=f'http://127.0.0.1:{server.server_port}'
   def post(path,data):
    request=urllib.request.Request(base+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Watch-Token':server.token})
    return json.load(urllib.request.urlopen(request,timeout=4))
   def run(value,time,day='2026-09-29'):
    q=quote(day=day,time=time,price=value,bid=value,ask=value+.01,lower_limit=1,upper_limit=100)
    return post('/api/paper/process',{'mode':'demo','quotes':[q],'session':session(day=day,time=time)})
   def paperstate():return json.load(urllib.request.urlopen(base+'/api/state'))['paper']
   try:
    post('/api/paper/calendar',calendar())
    post('/api/paper/decision',decision(quantity=200))
    self.assertEqual(post('/api/paper/process',{'mode':'demo','quotes':[quote()],'session':session()})['filled'],1)
    data={'plan_id':'http-integration','mode':'demo','symbol':'DEMO01','submitted_at':at(time='10:00:01'),'source':'user','reason':'纯虚构 HTTP 测试','quantity':200,'reference_price':10,'stages':[{'gain_pct':.1,'quantity':100}],'trailing':{'activation_gain_pct':.1,'distance_pct':.05},'test_only':True}
    self.assertEqual(post('/api/paper/exit-plan',data)['status'],'active')
    self.assertEqual(run(11,'10:00:00')['filled'],0)
    state=paperstate();plan=state['exit_plans'][0]
    self.assertEqual(plan['remaining_quantity'],200)
    self.assertEqual(float(plan['high_water_mark']),11)
    self.assertIsNotNone(plan['pending_decision_id'])
    self.assertIn('exit_disclaimer',state);self.assertTrue(state['exit_events'])
    self.assertEqual(run(11.2,'10:00:01')['filled'],1)
    self.assertEqual(paperstate()['exit_plans'][0]['remaining_quantity'],100)
    cancel={'mode':'demo','plan_id':'http-integration','reason':'结束此纯模拟测试'}
    self.assertEqual(post('/api/paper/exit-cancel',cancel)['status'],'cancelled')
    self.assertEqual(post('/api/paper/exit-cancel',cancel)['status'],'cancelled')
    self.assertEqual(paperstate()['exit_plans'][0]['status'],'cancelled')
    invalid=dict(data,plan_id='not-test-only',test_only=False)
    with self.assertRaises(urllib.error.HTTPError) as ctx:post('/api/paper/exit-plan',invalid)
    self.assertEqual(ctx.exception.code,400)
   finally:server.shutdown();server.server_close();worker.join(timeout=5)
 def test_static_traversal_absent(self):
  with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(self.base+'/../investment_watch.py')
  self.assertEqual(ctx.exception.code,404)
 def test_fresh_local_host_only(self):
  with self.assertRaises(urllib.error.HTTPError) as ctx:self.post('/api/run',{'mode':'demo'},extra={'Host':'example.com'})
  self.assertEqual(ctx.exception.code,403)

if __name__=='__main__':unittest.main()
