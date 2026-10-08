import base64
import hashlib
import json
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.service import App
from app.views import records_page
from jiaowopay_ingest import import_files, save_bundle
from jiaowopay_review.store import Conflict, ReviewError


class StageOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = App(self.root / 'workspace', ocr=False)
        self.app.manage({**self.app.catalog.context(), 'action': 'create', 'name': '合成账本'})

    def tearDown(self):
        self.temp.cleanup()

    def import_text(self, text, name='sample.csv'):
        self.app.start_import({**self.app.catalog.context(), 'files': [{'name': name, 'data': base64.b64encode(text.encode()).decode()}]})
        deadline = time.monotonic() + 10
        while self.app.job['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.app.job['status'], 'completed')

    def test_overview_does_not_claim_personal_spend_and_separates_currency(self):
        self.import_text('交易日期,金额,收支,币种,交易状态\n2026-01-01,0.1,支出,CNY,交易成功\n2026-01-02,0.2,支出,CNY,交易关闭\n2026-01-03,5,收入,USD,交易成功\n')
        data = self.app.overview()
        self.assertIsNone(data['personal_expense'])
        self.assertFalse(data['transaction_deduplication'])
        self.assertEqual({g['currency']:g['out'] for g in data['sources']}, {'CNY':'0.3', 'USD':'0'})
        self.assertEqual(data['sources'][0]['statuses']['void'], 1)

    def test_partial_preview_never_swallows_bad_records(self):
        self.import_text('交易日期,金额,收支\n2026-01-01,10,支出\n2026-01-02,xx,收入\n')
        overview = self.app.overview()
        self.assertEqual(overview['status'], 'partial')
        self.assertEqual(overview['summary']['eligible'], 1)
        self.assertEqual(overview['summary']['pending'], 1)
        self.assertEqual(overview['currency_default_count'], 2)

    def test_records_paginate_whole_book_not_first_500(self):
        body = '交易日期,金额,收支\n' + ''.join(f'2026-01-01,{i+1},支出\n' for i in range(510))
        self.import_text(body)
        first = self.app.batch_id
        self.import_text('交易日期,金额,收支\n2026-02-01,888,收入\n', 'second.csv')
        page = self.app.records({'page':['6'], 'page_size':['100']})
        self.assertEqual(page['total'], 511)
        self.assertEqual(len(page['records']), 11)
        self.assertTrue(all(r['batch_id'] == first for r in page['records']))
        matching = self.app.records({'q':['888']})
        self.assertEqual(matching['total'], 1)
        self.assertEqual(matching['records'][0]['amount'], '888')

    def test_unreadable_batch_has_unknown_count_and_no_fake_zero(self):
        self.import_text('交易日期,金额,收支\n2026-01-01,10,支出\n')
        broken = self.app.batch_id
        self.app.store.source_db.write_bytes(b'broken synthetic')
        overview = self.app.overview()
        self.assertEqual(overview['status'], 'partial')
        self.assertEqual(overview['unreadable_batch_ids'], [broken])
        self.assertEqual(overview['sources'], [])
        self.assertEqual(self.app.records({})['unreadable_batches'], [broken])

    def test_ui_action_retry_is_idempotent_and_stale_write_fails(self):
        self.import_text('交易日期,金额,收支\n2026-01-01,10,支出\n')
        source_hash = hashlib.sha256(self.app.store.source_db.read_bytes()).hexdigest()
        cmd = {**self.app.catalog.context(), 'batch_id': self.app.batch_id, 'workspace_id': self.app.store.workspace_id,
               'expected_revision': 0, 'request_id': uuid.uuid4().hex, 'note':'已对照合成来源', 'action':'confirm',
               'record_id': next(iter(self.app.store.originals)), 'fields': {'amount':'12'}}
        self.assertFalse(self.app.apply(cmd)['replayed'])
        self.assertTrue(self.app.apply(cmd)['replayed'])
        with self.assertRaises(Conflict):
            self.app.apply({**cmd, 'request_id':uuid.uuid4().hex})
        self.assertEqual(hashlib.sha256(self.app.store.source_db.read_bytes()).hexdigest(), source_hash)

    def test_confirmation_without_note_is_audited_replayable_and_undoable(self):
        self.import_text('交易日期,金额,收支\n2026-01-01,xx,支出\n')
        store = self.app.store
        original_hash = hashlib.sha256(store.source_db.read_bytes()).hexdigest()
        cmd = dict(workspace_id=store.workspace_id, expected_revision=0, request_id=uuid.uuid4().hex,
                   action='confirm', record_id=next(iter(store.originals)), fields={'amount':'12'})
        store.apply(cmd)  # Omitted note also works for direct API clients.
        self.assertTrue(store.apply(cmd)['replayed'])
        snap = store.snapshot()
        self.assertEqual(snap['summary']['pending'], 0)
        self.assertTrue(snap['history'][0]['changes'])
        self.assertNotIn('note', snap['history'][0])
        store.apply(dict(workspace_id=store.workspace_id, expected_revision=1, request_id=uuid.uuid4().hex,
                         action='undo', target_revision=1, note='撤销合成确认'))
        self.assertEqual(store.snapshot()['summary']['pending'], 1)
        self.assertEqual(hashlib.sha256(store.source_db.read_bytes()).hexdigest(), original_hash)
        cmd.update(expected_revision=2, request_id=uuid.uuid4().hex, note='')
        store.apply(cmd)
        self.assertEqual(store.snapshot()['history'][0]['note'], '')
        for bad in [None, 1, 'a'*2001]:
            with self.assertRaises(ReviewError):
                store.apply({**cmd, 'note':bad})

    def test_progress_counts_failed_and_skipped_files_without_losing_outcomes(self):
        file = self.root / 'valid.csv'
        file.write_text('交易日期,金额,收支\n2026-01-01,10,支出\n', encoding='utf8')
        events = []
        dataset = import_files([file, file, self.root/'missing.csv'], ocr=False, progress=events.append)
        self.assertEqual([f['status'] for f in dataset['files']], ['completed','duplicate_file_skipped','failed'])
        self.assertEqual([e['completed'] for e in events if e['stage']=='file_done'], [1,2,3])
        self.assertTrue(all(e['total']==3 for e in events))
        self.assertTrue(any(e['stage']=='standardizing' for e in events))

    def test_progress_complete_only_after_catalog_attached(self):
        seen = []
        save = self.app.save_job
        def observe(job):
            if job['status']=='completed':
                self.assertEqual(len(self.app.catalog.batches(job['book_id'])), 1)
            seen.append(dict(job))
            save(job)
        with patch.object(self.app, 'save_job', side_effect=observe):
            self.import_text('交易日期,金额,收支\n2026-01-01,10,支出\n')
        self.assertIn('saving', [j.get('progress',{}).get('stage') for j in seen if j['status']=='running'])
        self.assertEqual(seen[-1]['progress']['completed'], 1)
        self.assertGreaterEqual(seen[-1]['finished_at'], seen[-1]['started_at'])

    def test_interrupted_unsaved_import_survives_restart(self):
        self.app.save_job({'status':'running','id':uuid.uuid4().hex,'book_id':self.app.catalog.context()['book_id']})
        resumed = App(self.app.root, ocr=False)
        self.assertEqual(resumed.job['status'], 'interrupted')
        self.assertFalse(resumed.catalog.batches(resumed.catalog.context()['book_id']))

    def test_complete_unattached_import_recovers_exactly_once(self):
        bid = self.app.catalog.context()['book_id']; jid = uuid.uuid4().hex
        source = self.root / 'source.csv'; source.write_text('交易日期,金额,收支\n2026-01-01,10,支出\n', encoding='utf-8')
        save_bundle(import_files([source],ocr=False), self.app.root/'books'/bid/('import-'+jid),excel=False)
        self.app.save_job({'status':'running','id':jid,'book_id':bid,'batch_name':'恢复批次'})
        resumed = App(self.app.root, ocr=False)
        self.assertEqual(resumed.job['status'], 'completed')
        self.assertEqual(len(resumed.catalog.batches(bid)), 1)
        self.assertEqual(len(App(self.app.root,ocr=False).catalog.batches(bid)), 1)

    def test_complete_attached_import_recovers_without_duplication(self):
        self.import_text('交易日期,金额,收支\n2026-01-01,10,支出\n')
        self.app.save_job({**self.app.job, 'status':'running'})
        resumed = App(self.app.root,ocr=False)
        self.assertEqual(resumed.job['status'], 'completed')
        self.assertEqual(len(resumed.catalog.batches(resumed.catalog.context()['book_id'])), 1)

    def test_invalid_pagination_rejected(self):
        for params in ({'page':['0']},{'page_size':['1000']},{'page':['nan']}):
            with self.assertRaises(ReviewError): self.app.records(params)

    def test_failure_after_save_is_recovered_on_restart(self):
        with patch.object(self.app.catalog, 'attach', side_effect=OSError('synthetic attach interruption')):
            text = '交易日期,金额,收支\n2026-01-01,10,支出\n'
            self.app.start_import({**self.app.catalog.context(), 'files':[{'name':'saved.csv','data':base64.b64encode(text.encode()).decode()}]})
            deadline = time.monotonic()+10
            while self.app.job['status']=='running' and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.app.job['status'],'interrupted')
        resumed=App(self.app.root,ocr=False)
        self.assertEqual(resumed.job['status'],'completed')
        self.assertEqual(resumed.overview()['summary']['total'],1)

    def test_core_runs_without_relation_module(self):
        self.assertNotIn('jiaowopay_reconcile', sys.modules)
        self.assertFalse((Path(__file__).resolve().parents[1]/'jiaowopay_reconcile').exists())


if __name__ == '__main__':
    unittest.main()
