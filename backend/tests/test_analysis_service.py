import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from seed_analysis_fixture import seed
from analysis.ai import PairingAI,AIError
from jiaowopay_review.store import Conflict,ReviewError


class AnalysisServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=seed(self.temp.name)

    def test_views_and_export_share_projection_and_source_fields(self):
        summary=self.app.analysis_view({})
        records=self.app.analysis_view({'section':['transactions']})
        exported=self.app.analysis_view({'section':['export']})
        self.assertEqual(summary['source_count'],5)
        self.assertEqual(summary['transaction_count'],4)
        self.assertEqual(records['total'],4)
        self.assertEqual(summary['totals'],exported['totals'])
        tx=next(t for t in exported['transactions'] if len(t['sources'])==2)
        self.assertEqual(tx['description'],'苹果与牛奶')
        self.assertEqual(len(tx['fields']['memo']['alternatives']),1)

    def test_pairing_view_includes_direction_and_business_context(self):
        result=self.app.analysis_view({'section':['candidates']})
        self.assertTrue(result['items'])
        for pair in result['items']:
            self.assertIn('review_reason',pair)
            for record in pair['records']:
                self.assertEqual(record['effective_direction'],'out')
                self.assertIn('trade_type',record)
                self.assertIn('raw_status',record)
                self.assertTrue(record['source_label'].startswith('原'))
                self.assertIn('transaction_at_precision',record)

    def test_mock_ai_job_preview_cache_and_no_financial_mutation(self):
        sent=[]
        def transport(key,payload):
            sent.append(payload)
            return {'decisions':[{'id':p['id'],'verdict':'same'} for p in payload['candidates']]},{}
        self.app.pairing_ai=PairingAI(self.app,transport)
        s=self.app.analysis_view({});ctx=self.app.catalog.context()
        body={**ctx,'action':'start','fingerprint':s['fingerprint'],'expected_revision':s['revision'],'api_key':'synthetic-key-only'}
        job=self.app.analysis_action(body,ai=True)
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        current=self.app.analysis_view({});self.assertEqual(current['jobs'][0]['status'],'completed')
        self.assertEqual(current['totals'],s['totals'])
        self.assertEqual(len(sent),1)
        serialized=json.dumps(sent,ensure_ascii=False)
        self.assertNotIn('order10002',serialized);self.assertNotIn('(1234)',serialized)
        self.app.analysis_action({**body,'api_key':''},ai=True)
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual(len(sent),1)
        candidates=self.app.analysis_view({'section':['candidates']})
        self.assertTrue(all(c['ai_verdict']=='same' for c in candidates['items']))

    def test_cancel_discards_pending_response(self):
        arrived=threading.Event();release=threading.Event()
        def transport(key,payload):
            arrived.set();release.wait(3)
            return {'decisions':[{'id':p['id'],'verdict':'same'} for p in payload['candidates']]},{}
        self.app.pairing_ai=PairingAI(self.app,transport);s=self.app.analysis_view({})
        job=self.app.analysis_action({**self.app.catalog.context(),'action':'start','fingerprint':s['fingerprint'],'expected_revision':0,'api_key':'synthetic-key'},ai=True)
        self.assertTrue(arrived.wait(3))
        self.app.analysis_action({**self.app.catalog.context(),'action':'cancel','job_id':job['id']},ai=True);release.set()
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        store=self.app.analysis_store(self.app.catalog.context()['book_id']);j=self.app.pairing_ai.jobs(store)[0]
        self.assertEqual(j['status'],'cancelled');self.assertEqual(j['results'],[])

    def test_old_context_and_running_import_rejected(self):
        ctx=self.app.catalog.context();self.app.catalog.create('另一账本')
        with self.assertRaises(Conflict):self.app.analysis_action({**ctx,'action':'reject'})
        self.app.job={'status':'running'}
        with self.assertRaises(Conflict):self.app.analysis_action({**self.app.catalog.context(),'action':'reject'})

    def test_changed_decision_discards_late_ai_response(self):
        arrived=threading.Event();release=threading.Event()
        def transport(key,payload):
            arrived.set();release.wait(3)
            return {'decisions':[{'id':p['id'],'verdict':'same'} for p in payload['candidates']]},{}
        self.app.pairing_ai=PairingAI(self.app,transport)
        s=self.app.analysis_view({});ctx=self.app.catalog.context()
        self.app.analysis_action({**ctx,'action':'start','fingerprint':s['fingerprint'],'expected_revision':0,'api_key':'synthetic-key'},ai=True)
        self.assertTrue(arrived.wait(3))
        p=self.app.analysis_view({'section':['candidates']})['items'][0]
        self.app.analysis_action({**ctx,'action':'reject','relation_id':p['id'],'request_id':'late-test','fingerprint':s['fingerprint'],'expected_revision':0})
        release.set();deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        store=self.app.analysis_store(ctx['book_id']);job=self.app.pairing_ai.jobs(store)[0]
        self.assertEqual(job['status'],'stale');self.assertEqual(job['results'],[])

    def test_unknown_ai_action_does_not_start_job(self):
        with self.assertRaises(ReviewError):
            self.app.analysis_action({**self.app.catalog.context(),'action':'unexpected'},ai=True)
        self.assertFalse(self.app.pairing_ai.running)

    def test_failure_diagnostics_reach_summary_and_persist(self):
        def transport(key,payload):raise AIError('http_401','HTTP 401：密钥认证失败。')
        self.app.pairing_ai=PairingAI(self.app,transport)
        s=self.app.analysis_view({})
        self.app.analysis_action({**self.app.catalog.context(),'action':'start','fingerprint':s['fingerprint'],'expected_revision':0,'api_key':'private-test-key'},ai=True)
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        self.app.pairing_ai=PairingAI(self.app,transport)
        job=self.app.analysis_view({})['jobs'][0]
        self.assertEqual(job['error']['code'],'http_401')
        self.assertEqual(job['error']['batch'],1)
        self.assertIn('HTTP 401',job['message'])
        self.assertNotIn('private-test-key',json.dumps(job))

    def test_truncation_splits_then_persists_success(self):
        sizes=[]
        def transport(key,payload):
            items=payload['candidates'];sizes.append(len(items))
            if len(items)>1:raise AIError('output_limit','测试截断')
            return {'decisions':[{'id':items[0]['id'],'verdict':'uncertain'}]},{}
        self.app.pairing_ai=PairingAI(self.app,transport)
        s=self.app.analysis_view({})
        self.app.analysis_action({**self.app.catalog.context(),'action':'start','fingerprint':s['fingerprint'],'expected_revision':0,'api_key':'private-test-key'},ai=True)
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        job=self.app.analysis_view({})['jobs'][0]
        self.assertEqual(sizes,[2,1,1])
        self.assertEqual(job['calls'],3)
        self.assertEqual(job['completed'],2)
        self.assertEqual(job['status'],'completed')

    def test_single_truncation_does_not_retry_forever(self):
        sizes=[]
        def transport(key,payload):
            sizes.append(len(payload['candidates']));raise AIError('output_limit','测试截断')
        self.app.pairing_ai=PairingAI(self.app,transport)
        s=self.app.analysis_view({})
        self.app.analysis_action({**self.app.catalog.context(),'action':'start','fingerprint':s['fingerprint'],'expected_revision':0,'api_key':'private-test-key'},ai=True)
        deadline=time.monotonic()+5
        while self.app.pairing_ai.running and time.monotonic()<deadline:time.sleep(.01)
        job=self.app.analysis_view({})['jobs'][0]
        self.assertEqual(sizes,[2,1])
        self.assertEqual(job['error']['code'],'output_limit')
        self.assertEqual(job['status'],'failed')

if __name__=='__main__':unittest.main()
