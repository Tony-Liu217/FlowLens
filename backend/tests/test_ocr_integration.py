import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from jiaowopay_ingest import import_files, save_bundle, load_records, load_review_items, resolve_evidence
from jiaowopay_ingest.model import ReadResult, Segment, Row, issue, ReadFailure
from jiaowopay_ingest.readers import _pdf, detect_format
from jiaowopay_ingest.ocr import asset
from jiaowopay_ocr.consensus import merge_observations
from jiaowopay_ocr.policy import assess, page_gate, MIN_SCORE
from jiaowopay_ocr.table import money, recover_currency_column, date_value, record


def fixture(sha, pending=False, no_count=False, currency_value='人民币', currency_score=.999):
    row={'transaction_at':'2026-01-01','amount':'0.01','direction':'out','balance':'99.99',
         'raw_fields':{'date':'2026-01-01','signed':'-0.01','balance':'99.99','currency':'人民币'},
         'issues':[], 'field_scores':{f:[.999] for f in ['date','signed','balance','currency']},
         'source_geometry':{'date_x':80,'date_y':100,'date_width':90,'row_height':40,
                            'row_y_bounds':[80,120],'columns':{'currency':[400,500]}}}
    row['raw_fields']['currency']=currency_value
    row['field_scores']['currency']=[currency_score]
    observations={k:[copy.deepcopy(row)] for k in ['original','dark_gray','dark_neutral']}
    if pending:observations['dark_gray'][0]['amount']='0.10'
    proposals,issues=merge_observations(observations,{})
    for p in proposals:p['review_crop']={'path':'row-00001.png','bbox':[0,70,800,130]}
    return {'observations':observations,'proposals':proposals,'proposal_issues':issues,
            'image_sha256':sha,'image_size':[800,500],'versions':{'test':'synthetic'},
            'raw_views':{'original':{'boxes':[[[0,200],[100,200],[100,220],[0,220]]],
                                     'texts':['说明' if no_count else '本页交易笔数：1']}}}


class OCRIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.source=self.root/'synthetic.pdf'
        self.source.write_bytes(b'%PDF-test-fixture')
        self.sha=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):self.temp.cleanup()

    def dataset(self,pending=False,no_count=False,fail=False,currency_value='人民币',currency_score=.999):
        result=ReadResult('pdf',[Segment('page-1',[],'scan',1)],
                          [issue('OCR_REQUIRED','needs OCR',page=1)])
        def worker(source,page,output,*args):
            output.mkdir()
            (output/'original.png').write_bytes(b'private-test-image')
            if fail:raise ReadFailure('OCR_TIMEOUT','bounded timeout')
            data=fixture(self.sha,pending,no_count,currency_value,currency_score)
            (output/'row-00001.png').write_bytes(b'private-test-row')
            (output/'result.json').write_text(json.dumps(data),encoding='utf-8')
            return data
        with patch('jiaowopay_ingest.pipeline.read_document',return_value=result),patch('jiaowopay_ingest.ocr.run_worker',side_effect=worker):
            return import_files([self.source])

    def test_currency_uncertainty_alone_does_not_create_review(self):
        for value, score, expected in [('', 0, 'CNY'), ('?', .1, 'CNY'), ('人民币', .1, 'CNY'), ('USD', .999, 'USD')]:
            with self.subTest(value=value):
                d=self.dataset(currency_value=value,currency_score=score)
                self.assertEqual(d['records'][0]['currency'],expected)
                self.assertEqual(d['records'][0]['parse_status'],'ready')
                self.assertEqual(d['reviews'],[])
                raw=d['raw_rows'][0]['ocr_original_fields']['currency']
                self.assertEqual(raw,value)

    def test_currency_default_keeps_amount_conflict_pending(self):
        d=self.dataset(pending=True,currency_value='?')
        self.assertEqual(d['records'][0]['currency'],'CNY')
        self.assertEqual(d['records'][0]['parse_status'],'needs_review')
        self.assertTrue(d['reviews'])

    def test_ready_gate_and_same_schema_storage(self):
        data=self.dataset()
        self.assertEqual(data['summary']['ready_count'],1)
        self.assertEqual(data['records'][0]['currency'],'CNY')
        self.assertEqual(data['reviews'],[])
        target=save_bundle(data,self.root/'out',excel=False)
        self.assertEqual(load_records(target),data['records'])
        self.assertEqual(load_review_items(target),[])

    def test_conflict_pending_row_crop_and_original_preserved(self):
        data=self.dataset(pending=True)
        self.assertEqual(data['summary']['needs_review_count'],1)
        self.assertIsNone(data['records'][0]['amount'])
        self.assertEqual(data['raw_rows'][0]['ocr_original_fields']['signed'],'-0.01')
        target=save_bundle(data,self.root/'out',excel=False)
        with self.assertRaises(ValueError):load_records(target)
        review=load_review_items(target)[0]
        self.assertEqual(resolve_evidence(target,review['evidence']['row_image']).read_bytes(),b'private-test-row')
        self.assertEqual(review['record_id'],data['records'][0]['record_id'])
        self.assertEqual(len(load_records(target,allow_partial=True,ready_only=False)),1)

    def test_no_count_creates_page_and_record_tasks(self):
        data=self.dataset(no_count=True)
        self.assertEqual({r['scope'] for r in data['reviews']},{'page','record'})
        self.assertEqual(data['summary']['ready_count'],0)

    def test_timeout_preserves_page_task(self):
        data=self.dataset(fail=True)
        self.assertEqual(data['records'],[])
        self.assertEqual(data['reviews'][0]['reason_codes'],['OCR_TIMEOUT'])
        self.assertIn('page_image',data['reviews'][0]['evidence'])

    def test_no_ocr_is_explicit(self):
        result=ReadResult('pdf',[Segment('page-1',[],'scan',1)],[issue('OCR_REQUIRED','pending',page=1)])
        with patch('jiaowopay_ingest.pipeline.read_document',return_value=result),patch('jiaowopay_ingest.ocr.run_worker') as worker:
            data=import_files([self.source],ocr=False)
        worker.assert_not_called()
        self.assertIn('OCR_REQUIRED',[p['code'] for p in data['issues']])

    def test_assets_move_with_bundle_and_tamper_detection(self):
        target=save_bundle(self.dataset(pending=True),self.root/'out',excel=False)
        moved=self.root/'copied'
        shutil.copytree(target,moved)
        relative=load_review_items(moved)[0]['evidence']['row_image']
        path=resolve_evidence(moved,relative)
        self.assertTrue(path.is_relative_to(moved))
        path.write_bytes(b'tampered')
        with self.assertRaises(ValueError):resolve_evidence(moved,relative)

    def test_path_traversal_and_unregistered_asset(self):
        target=save_bundle(self.dataset(),self.root/'out',excel=False)
        for path in ['../secret','evidence/../../secret','C:/secret','evidence\\secret','evidence/absent.png']:
            with self.assertRaises(ValueError):resolve_evidence(target,path)

    def test_missing_asset_prevents_complete_bundle(self):
        data=self.dataset(pending=True)
        data['_assets'].clear()
        with self.assertRaises(ValueError):save_bundle(data,self.root/'out',excel=False)
        self.assertFalse((self.root/'out/COMPLETE').exists())

    def test_review_preview_is_escaped_and_read_only(self):
        data=self.dataset(pending=True)
        data['reviews'][0]['suggested_fields']['currency']='<script>evil()</script>'
        target=save_bundle(data,self.root/'out',excel=False)
        html=(target/'review.html').read_text(encoding='utf-8')
        self.assertNotIn('<script>',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn("form-action 'none'",html)

    def test_legacy_bundle_remains_readable(self):
        import sqlite3
        from contextlib import closing
        data=self.dataset()
        target=save_bundle(data,self.root/'legacy',excel=False)
        with closing(sqlite3.connect(target/'ledger.sqlite3')) as con, con:
            con.execute("UPDATE manifest SET value=? WHERE key='schema_version'",(json.dumps('1.0'),))
            con.execute('DROP TABLE reviews')
        self.assertEqual(len(load_records(target)),1)
        self.assertEqual(load_review_items(target),[])

    def test_asset_budget_fails_closed(self):
        data={'_assets':{},'assets':[]}
        with patch('jiaowopay_ingest.ocr.MAX_ASSET_BYTES',2):
            with self.assertRaises(ReadFailure):asset(data,'evidence/a',b'abc','test')

    def test_ids_stable_across_batches(self):
        a,b=self.dataset(pending=True),self.dataset(pending=True)
        self.assertEqual(a['reviews'][0]['review_id'],b['reviews'][0]['review_id'])

    def test_native_and_scan_processed_once(self):
        native=Segment('table',[Row(['交易日期','金额','收支','币种'],1),Row(['2026-01-01','10','支出','CNY'],2)],'pdf_table',1,1)
        result=ReadResult('pdf',[native,Segment('scan',[],'scan',2)])
        with patch('jiaowopay_ingest.pipeline.read_document',return_value=result),patch('jiaowopay_ingest.ocr.import_page') as ocr:
            data=import_files([self.source])
        self.assertEqual(len(data['records']),1)
        ocr.assert_called_once()
        self.assertEqual(ocr.call_args.args[5],2)

    def test_raster_page_with_text_header_is_ocr_once(self):
        page=MagicMock()
        page.chars=[{'text':'title'}]
        page.find_tables.return_value=[]
        page.width=100;page.height=100;page.page_number=1
        page.images=[{'width':100,'height':95}]
        pdf=MagicMock();pdf.pages=[page]
        with patch('pdfplumber.open') as op:
            op.return_value.__enter__.return_value=pdf
            result=_pdf(self.source)
        self.assertEqual([s.kind for s in result.segments],['scan'])
        page.extract_text.assert_not_called()

    def test_content_based_image_detection(self):
        path=self.root/'misnamed.bin';path.write_bytes(b'\x89PNG\r\n\x1a\n')
        self.assertEqual(detect_format(path),'png')

    def test_text_pdf_without_table_also_uses_ocr_fallback(self):
        page=MagicMock();page.chars=[{'text':'日期'}]
        page.find_tables.return_value=[]
        page.images=[];page.page_number=1
        pdf=MagicMock();pdf.pages=[page]
        with patch('pdfplumber.open') as op:
            op.return_value.__enter__.return_value=pdf
            result=_pdf(self.source)
        self.assertEqual([s.kind for s in result.segments],['scan'])


class PolicyTests(unittest.TestCase):
    def test_threshold_is_not_only_gate(self):
        result=fixture('sha',pending=True)
        self.assertTrue(assess(result,0,{'transaction_at':'2026-01-01','amount':None,'direction':None,'currency':'CNY','balance':'99.99'}))

    def test_low_score_blocks(self):
        result=fixture('sha')
        result['observations']['original'][0]['field_scores']['signed']=[MIN_SCORE-.001]
        normalized={**result['proposals'][0],'currency':'CNY'}
        self.assertIn('OCR_SCORE_BELOW_THRESHOLD',assess(result,0,normalized))

    def test_count_mismatch(self):
        result=fixture('sha')
        result['raw_views']['original']['texts']=['本页交易笔数：2']
        self.assertIn('OCR_PAGE_COUNT_MISMATCH',page_gate(result)[0])

    def test_defaulted_currency_does_not_block_ocr(self):
        result=fixture('sha',currency_value='?',currency_score=.1)
        normalized={**result['proposals'][0],'currency':'CNY'}
        self.assertEqual(assess(result,0,normalized),[])

    def test_currency_header_recovery_needs_two_locations_and_original_text(self):
        result=fixture('sha')
        rows=result['observations']
        del rows['original'][0]['raw_fields']['currency']
        raw={'boxes':[[[420,90],[480,90],[480,110],[420,110]]],'texts':['USD'],'scores':[.999]}
        recovery=recover_currency_column(raw,rows)
        self.assertIsNotNone(recovery)
        self.assertEqual(rows['original'][0]['raw_fields']['currency'],'USD')

    def test_currency_header_conflict_no_recovery(self):
        result=fixture('sha');rows=result['observations']
        del rows['original'][0]['raw_fields']['currency']
        rows['dark_gray'][0]['source_geometry']['columns']['currency']=[500,600]
        self.assertIsNone(recover_currency_column({},rows))

    def test_money_grouping_not_guessed(self):
        self.assertIsNone(money('1,13.08'))
        self.assertIsNone(money('1.137.08'))
        self.assertEqual(str(money('-1,137.08')),'-1137.08')

    def test_damaged_time_not_reduced_to_date(self):
        self.assertIsNone(date_value('2026-01-01 12:O3:04'))
        self.assertIsNone(date_value('watermark 2026-01-01'))
        self.assertEqual(date_value('2026-01-01 12:03:04'),'2026-01-01 12:03:04')

    def test_negative_split_is_not_silently_abs(self):
        r=record({'date':'2026-01-01','credit':'-10.00','balance':'99.00'})
        self.assertIsNone(r['amount'])
        self.assertIn('SPLIT_AMOUNT_NEGATIVE',r['issues'])


if __name__=='__main__':unittest.main()
