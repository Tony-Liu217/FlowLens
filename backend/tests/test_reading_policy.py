import copy
import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jiaowopay_ingest import import_files, save_bundle
from jiaowopay_ingest.mapping import Mapping, document_profile, find_header
from jiaowopay_ingest.model import Row, ReadResult, Segment
from jiaowopay_ingest.normalize import normalize, parse_money
from jiaowopay_ocr.consensus import merge_observations
from jiaowopay_ocr.policy import assess, auxiliary
from jiaowopay_ocr.table import record
from jiaowopay_review.store import ReviewStore
import test_ocr_integration as ocr_fixture

HEADERS = ['序号','摘要','交易日期','交易金额','账户余额','交易地点/附言','对方账号与户名']
TITLE = '中国建设银行个人活期账户全部交易明细'


class ReadingPolicyTests(unittest.TestCase):
    def test_currency_sign_variants_and_invalid_tokens(self):
        for token in ['-￥1,234.50','￥-1,234.50','（￥1,234.50）','人民币负1,234.50','1,234.50-','−1234.50']:
            with self.subTest(token=token): self.assertEqual(parse_money(token), Decimal('-1234.50'))
        for token in ['+￥1,234.50','￥+1,234.50','￥1,234.50','1234.50元','USD +1,234.50']:
            with self.subTest(token=token): self.assertEqual(parse_money(token), Decimal('1234.50'))
        for token in ['￥-+12','(-12)','USD ￥12','1,23.45','1.234,50','12abc','NaN','1e10','1 234.50','--12']:
            with self.subTest(token=token): self.assertIsNone(parse_money(token))

    def test_unsigned_generic_never_infers_income_from_description(self):
        mapping=find_header([Row(['交易日期','交易金额','摘要'],1)])
        for description in ['消费退货','工资','转入','退款']:
            rec,problems=normalize(Row(['20260101','￥366.00',description],2),mapping)
            self.assertIsNone(rec['direction'])
            self.assertIn('DIRECTION_MISSING',[p['code'] for p in problems])
        for value, direction in [('+￥366','in'),('￥-366','out'),('(￥366)','out')]:
            rec,_=normalize(Row(['20260101',value,''],2),mapping)
            self.assertEqual(rec['direction'],direction)

    def test_explicit_direction_and_conflicting_evidence(self):
        tests=[(['交易日期','金额','收支'],['20260101','￥10','支出'],'ready','out'),
               (['交易日期','金额','收支'],['20260101','+￥10','支出'],'needs_review','out'),
               (['交易日期','有符号金额','收入金额'],['20260101','-10','10'],'needs_review','in'),
               (['交易日期','金额','收入金额'],['20260101','-￥10','10'],'needs_review','in'),
               (['交易日期','金额','有符号金额'],['20260101','-10','+10'],'needs_review','in'),
               (['交易日期','收入金额','支出金额'],['20260101','0','￥10'],'ready','out'),
               (['交易日期','收入金额','支出金额'],['20260101','5','10'],'needs_review',None)]
        for headers, values, status, direction in tests:
            with self.subTest(headers=headers,values=values):
                rec,_=normalize(Row(values,2),find_header([Row(headers,1)]))
                self.assertEqual((rec['parse_status'],rec['direction']),(status,direction))

    def test_ccb_pdf_requires_bank_heading_and_complete_structure(self):
        for title,headers,expected in [(TITLE,HEADERS,True),('',HEADERS,False),('其他银行',HEADERS,False),(TITLE,HEADERS[:-1],False)]:
            mapping=document_profile(find_header([Row(headers,1)]),title)
            self.assertEqual(mapping.signed,expected)
        mapping=Mapping({'transaction_at':2,'amount':3},HEADERS,0,confirmed=True)
        self.assertFalse(document_profile(mapping,TITLE).signed)

    def test_currency_codes_in_amount_are_not_discarded(self):
        for amount, explicit, currency, expected in [('+USD 10','','USD','ready'),('EUR +10','EUR','EUR','ready'),('￥10','JPY','JPY','ready'),('USD 10','CNY','CNY','needs_review'),('£10','','GBP','ready')]:
            mapping=find_header([Row(['交易日期','金额','收支','币种'],1)])
            row,_=normalize(Row(['20260101',amount,'收入',explicit],2),mapping)
            self.assertEqual((row['currency'],row['parse_status']),(currency,expected))

    def test_ocr_currency_decoration_and_split_zero(self):
        for values,expected in [({'signed':'￥-10'},'out'),({'signed':'+￥10'},'in'),({'signed':'￥10'},None),({'credit':'0','debit':'￥10'},'out'),({'credit':'10','debit':'0'},'in'),({'credit':'abc','debit':'10'},None),({'credit':'10','debit':'5'},None)]:
            with self.subTest(values=values):
                self.assertEqual(record({'date':'2026-01-01','balance':'1',**values})['direction'],expected)

    def test_balance_only_uncertainty_is_nonblocking_but_preserved(self):
        for kind in ['low_score','missing','conflict','recovered']:
            with self.subTest(kind=kind):
                result=ocr_fixture.fixture('sha')
                original=result['observations']['original'][0]
                if kind=='low_score':
                    original['field_scores']['balance']=[.5];original['issues']=['LOW_OCR_SCORE']
                if kind in {'missing','recovered'}:
                    original['balance']=None;original['issues']=['BALANCE_UNREADABLE'];original['field_scores']['balance']=[]
                if kind=='missing':
                    for rows in result['observations'].values():rows[0]['balance']=None
                if kind=='conflict':result['observations']['dark_gray'][0]['balance']='555.00'
                result['proposals'],result['proposal_issues']=merge_observations(result['observations'],{})
                self.assertEqual(assess(result,0,{**result['proposals'][0],'currency':'CNY'}),[])
                self.assertFalse(auxiliary(result,0)['balance_reliable'])

    def test_critical_uncertainty_and_page_integrity_still_block(self):
        for kind in ['amount_score','date_score','amount_conflict','direction_lost','date_recovered','page_count','unknown_issue']:
            with self.subTest(kind=kind):
                result=ocr_fixture.fixture('sha')
                original=result['observations']['original'][0]
                if kind=='amount_score':original['field_scores']['signed']=[.9799]
                if kind=='date_score':original['field_scores']['date']=[]
                if kind=='amount_conflict':result['observations']['dark_gray'][0]['amount']='10'
                if kind=='direction_lost':result['observations']['dark_gray'][0]['direction']=None
                if kind=='date_recovered':original['transaction_at']=None
                if kind=='page_count':result['raw_views']['original']['texts']=['本页交易笔数：2']
                if kind=='unknown_issue':original['issues']=['UNSUPPORTED_CRITICAL_PROBLEM']
                result['proposals'],result['proposal_issues']=merge_observations(result['observations'],{})
                self.assertTrue(assess(result,0,{**result['proposals'][0],'currency':'CNY'}))

    def test_old_ccb_overlay_is_immutable_and_human_override_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'any-name.pdf';source.write_bytes(b'%PDF-synthetic')
            doc=ReadResult('pdf',[Segment('t',[Row(HEADERS,1),Row(['1','消费退货','20260101','366','1000','',''],2)],'pdf_table',1),
                                  Segment('text',[Row([TITLE],1)],'pdf_text',1)])
            with patch('jiaowopay_ingest.pipeline.read_document',return_value=doc),patch('jiaowopay_ingest.pipeline.document_profile',side_effect=lambda m,t:m):
                data=import_files([source],ocr=False)
            bundle=save_bundle(data,root/'bundle',excel=False)
            before=hashlib.sha256((bundle/'ledger.sqlite3').read_bytes()).hexdigest()
            store=ReviewStore(bundle,root/'states');snap=store.snapshot();rid=snap['records'][0]['record_id']
            self.assertIsNone(store.originals[rid]['direction'])
            self.assertEqual(snap['records'][0]['direction'],'in')
            self.assertEqual(snap['summary']['pending'],0)
            store.apply(dict(workspace_id=store.workspace_id,expected_revision=0,request_id=uuid.uuid4().hex,action='confirm',record_id=rid,fields={'amount':'365','direction':'out'},note=''))
            self.assertEqual(ReviewStore(bundle,root/'states').snapshot()['records'][0]['direction'],'out')
            store.apply(dict(workspace_id=store.workspace_id,expected_revision=1,request_id=uuid.uuid4().hex,action='undo',target_revision=1,note='撤销'))
            self.assertEqual(store.snapshot()['records'][0]['direction'],'in')
            self.assertEqual(hashlib.sha256((bundle/'ledger.sqlite3').read_bytes()).hexdigest(),before)

    def test_old_ocr_overlay_releases_only_balance_tasks(self):
        case=ocr_fixture.OCRIntegrationTests();case.setUp()
        try:
            real_fixture=ocr_fixture.fixture
            def balance_fixture(*args,**kwargs):
                result=real_fixture(*args,**kwargs)
                result['observations']['original'][0]['field_scores']['balance']=[.5]
                return result
            with patch.object(ocr_fixture,'fixture',side_effect=balance_fixture),patch('jiaowopay_ingest.ocr.assess',return_value=['OCR_SCORE_BELOW_THRESHOLD']):
                data=case.dataset()
            data['records'][0]['ocr_policy']='ocr-review-2-cny-default'
            bundle=save_bundle(data,case.root/'legacy',excel=False)
            before=hashlib.sha256((bundle/'ledger.sqlite3').read_bytes()).hexdigest()
            snap=ReviewStore(bundle,case.root/'states').snapshot()
            self.assertEqual(snap['summary']['pending'],0)
            self.assertEqual(snap['summary']['blocking_issues'],0)
            self.assertEqual(snap['tasks'][0]['status'],'resolved_by_policy')
            self.assertFalse(snap['records'][0]['balance_reliable'])
            self.assertFalse(snap['records'][0].get('human_confirmed',False))
            self.assertEqual(hashlib.sha256((bundle/'ledger.sqlite3').read_bytes()).hexdigest(),before)
        finally:case.tearDown()


if __name__=='__main__':unittest.main()
