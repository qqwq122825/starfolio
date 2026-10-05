"""Independent analyst evidence fields must never become inferred trading signals."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import investment_watch as w


class NewsAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'test.sqlite'
        self.conn = w.connect(self.db)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def sample(self, **kw):
        result = {'symbol': '600000', 'title': '仅测试的事件',
                  'source_url': 'https://www.sse.com.cn/', 'source': '测试来源',
                  'published_at': w.stamp()}
        result.update(kw)
        return result

    def test_default_unknown_hold_no_paper_decision(self):
        result = w.add_news(self.conn, self.sample())
        self.assertEqual(result['stage'], 'unknown')
        self.assertEqual(result['expectation_status'], 'unknown')
        self.assertEqual(result['price_reaction'], 'unknown')
        self.assertEqual(result['research_assessment']['action'], 'HOLD')
        self.assertEqual(result['research_assessment']['status'], 'insufficient_evidence')
        self.assertIn('factual_stage_evidence', result['research_assessment']['missing_fields'])
        self.assertEqual(w.paper.paper_state(self.conn, 'imported')['decisions'], [])

    def test_each_classification_needs_its_own_evidence(self):
        for field, choices in [('stage', ['application', 'pilot', 'order', 'revenue']),
                               ('expectation_status', ['above', 'in_line', 'below', 'priced_in']),
                               ('price_reaction', ['up', 'flat', 'down'])]:
            for value in choices:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    w.add_news(self.conn, self.sample(**{field: value}))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM news').fetchone()[0], 0)

    def test_price_cannot_substitute_for_factual_excerpt(self):
        with self.assertRaisesRegex(ValueError, '价格反应不能'):
            w.add_news(self.conn, self.sample(stage='order', price_reaction='up',
                       price_reaction_evidence='测试价格从 10 到 12',
                       expectation_status='above', expectation_evidence='测试比较基准'))

    def test_price_evidence_alone_does_not_infer_facts_or_expectations(self):
        result = w.add_news(self.conn, self.sample(price_reaction='up',
                           price_reaction_evidence='测试观察区间价格从 10 到 12',
                           research_assessment={'action': 'BUY'}, factual_proof=True))
        self.assertEqual(result['stage'], 'unknown')
        self.assertEqual(result['evidence_excerpt'], '')
        self.assertEqual(result['expectation_status'], 'unknown')
        self.assertEqual(result['research_assessment']['action'], 'HOLD')
        self.assertNotIn('factual_proof', result)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0], 0)

    def test_complete_human_record_still_review_only_not_buy_or_sell(self):
        result = w.add_news(self.conn, self.sample(
            stage='order', evidence_excerpt='合同原文的测试摘录，不是真实公告',
            verification='reviewed', reviewed_by='测试研究者',
            expectation_status='above', expectation_evidence='事前基准、出处与比较的测试记录',
            price_reaction='down', price_reaction_evidence='报价源和前后观察时间的测试记录'))
        assessment = result['research_assessment']
        self.assertEqual(assessment['action'], 'HOLD')
        self.assertEqual(assessment['status'], 'manual_review_only')
        self.assertEqual(assessment['missing_fields'], [])
        self.assertEqual(result['stage'], 'order')
        self.assertEqual(result['expectation_status'], 'above')
        self.assertEqual(result['price_reaction'], 'down')
        alert = json.loads(self.conn.execute('SELECT payload FROM alerts').fetchone()[0])
        self.assertEqual(alert['research_assessment'], assessment)
        self.assertEqual(w.paper.paper_state(self.conn, 'imported')['decisions'], [])
        self.assertIn('HOLD', w.digest(self.conn, 'imported'))

    def test_invalid_classification_types_and_evidence_rejected(self):
        for invalid in [None, {}, [], False, 5, 'automatic']:
            for field in ['stage', 'expectation_status', 'price_reaction']:
                with self.subTest(field=field, invalid=invalid), self.assertRaises(ValueError):
                    w.add_news(self.conn, self.sample(**{field: invalid}))
        for field in ['evidence_excerpt', 'expectation_evidence', 'price_reaction_evidence']:
            for invalid in [None, 1, [], {}, 'x' * 3001]:
                with self.subTest(field=field), self.assertRaises(ValueError):
                    w.add_news(self.conn, self.sample(**{field: invalid}))

    def test_legacy_payload_migrates_without_fabricating_evidence(self):
        legacy = self.sample(id='legacy', mode='imported', stage='order',
                             verification='reviewed', reviewed_by='旧记录研究者',
                             evidence_excerpt='旧订单原文', note='保留这段原始备注', amount='10')
        self.conn.execute('INSERT INTO news VALUES(?,?,?,?,?,?,?,?)',
                          ('legacy', '600000', 'imported', legacy['title'], legacy['published_at'],
                           'order', 'reviewed', json.dumps(legacy, ensure_ascii=False)))
        self.conn.execute("DELETE FROM settings WHERE key='news_analysis_schema'")
        self.conn.commit()
        self.conn.close()
        self.conn = w.connect(self.db)
        stored = json.loads(self.conn.execute("SELECT payload FROM news WHERE id='legacy'").fetchone()[0])
        self.assertEqual(stored['stage'], 'order')
        self.assertEqual(stored['evidence_excerpt'], '旧订单原文')
        self.assertEqual(stored['note'], legacy['note'])
        self.assertEqual(stored['expectation_status'], 'unknown')
        self.assertEqual(stored['price_reaction'], 'unknown')
        self.assertEqual(stored['research_assessment']['action'], 'HOLD')
        self.assertEqual(stored['research_assessment']['status'], 'insufficient_evidence')
        w.set_setting(self.conn, 'mode', 'imported')
        self.assertEqual(w.state(self.conn)['news'][0], stored)

    def test_demo_never_fabricates_expectation_or_price_evidence(self):
        w.run_monitor(self.conn, 'demo')
        for n in w.state(self.conn)['news']:
            self.assertEqual(n['expectation_status'], 'unknown')
            self.assertEqual(n['price_reaction'], 'unknown')
            self.assertEqual(n['research_assessment']['action'], 'HOLD')

    def test_downgrade_refreshes_alert_gate_preserving_review_history(self):
        w.add_news(self.conn, self.sample(stage='order', evidence_excerpt='测试订单原文',
                   verification='reviewed', reviewed_by='研究者'))
        self.conn.execute("UPDATE alerts SET status='reviewed'")
        result=w.add_news(self.conn, self.sample())
        row=self.conn.execute('SELECT * FROM alerts').fetchone()
        payload=json.loads(row['payload'])
        self.assertEqual(row['status'],'reviewed')
        self.assertEqual(row['score'],0)
        self.assertFalse(payload['event_currently_eligible'])
        self.assertEqual(payload['research_assessment'],result['research_assessment'])
        self.assertEqual(payload['research_assessment']['status'],'insufficient_evidence')
        self.assertIn('不再满足',payload['hypothesis'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM alerts').fetchone()[0],1)

    def test_invalid_legacy_new_fields_fall_back_to_unknown(self):
        n = w.news_with_analysis({'stage': 'unknown', 'expectation_status': [],
                                 'price_reaction': 'guessed', 'expectation_evidence': None})
        self.assertEqual(n['expectation_status'], 'unknown')
        self.assertEqual(n['price_reaction'], 'unknown')
        self.assertEqual(n['expectation_evidence'], '')
        self.assertEqual(n['research_assessment']['action'], 'HOLD')


class ExitCLIRoutingTests(unittest.TestCase):
    def test_exit_plan_and_cancel_route(self):
        for command, method, data in [
            ('paper-exit-plan', 'configure_exit_plan', {'mode': 'demo', 'plan_id': 'test'}),
            ('paper-exit-cancel', 'cancel_exit_plan', {'mode': 'demo', 'plan_id': 'test', 'reason': '测试取消'}),
        ]:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'input.json'
                path.write_text(json.dumps(data), encoding='utf-8')
                with mock.patch.object(w.paper, method, return_value={'ok': True}, create=True) as called, \
                     mock.patch.object(sys, 'argv', ['investment_watch.py', '--db', str(Path(tmp)/'db'), command, str(path)]), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(w.main(), 0)
                    self.assertTrue(json.loads(stdout.getvalue())['ok'])
                    called.assert_called_once()
                    if command == 'paper-exit-plan':
                        self.assertEqual(called.call_args.args[1], data)
                    else:
                        self.assertEqual(called.call_args.args[1:], ('demo', 'test', '测试取消'))


if __name__ == '__main__':
    unittest.main()
