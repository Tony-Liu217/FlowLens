from __future__ import annotations

import copy
import hashlib
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '.deps'))
from jiaowopay_ingest import import_files, save_bundle
from jiaowopay_ingest.model import issue
from jiaowopay_review.store import ReviewStore, ReviewError, Conflict, load_effective_records
from app.server import App, Server, safe_filename


def sample(root):
    source = root / 'synthetic.csv'
    source.write_text('交易时间,金额,收支,币种,摘要\n2026-01-02,12.30,支出,人民币,午餐\n2026-01-03,xx,收入,人民币,待核对\n', encoding='utf-8')
    return import_files([source], ocr=False)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dataset = sample(self.root)
        self.bundle = save_bundle(self.dataset, self.root / 'bundle', excel=False)
        self.states = self.root / 'states'
        self.store = ReviewStore(self.bundle, self.states)
        self.pending = self.dataset['records'][1]['record_id']
        self.ready = self.dataset['records'][0]['record_id']
        self.hashes = {str(p.relative_to(self.bundle)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.bundle.rglob('*') if p.is_file()}

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, **values):
        return {'workspace_id': self.store.workspace_id, 'expected_revision': self.store.snapshot()['revision'],
                'request_id': str(uuid.uuid4()), 'note': '合成数据测试核验', **values}

    def confirm(self, rid=None, **fields):
        return self.store.apply(self.command(action='confirm', record_id=rid or self.pending, fields=fields or {'amount': '20.00'}))

    def test_pending_structured_record_gets_task_and_blocks_export(self):
        snap = self.store.snapshot()
        self.assertEqual(snap['summary']['pending'], 1)
        self.assertEqual(len(snap['tasks']), 1)
        with self.assertRaises(ReviewError): load_effective_records(self.bundle, self.states)
        result = load_effective_records(self.bundle, self.states, allow_partial=True)
        self.assertTrue(result['partial'])
        self.assertEqual(len(result['records']), 1)

    def test_confirm_persist_and_original_unchanged(self):
        self.confirm()
        result = load_effective_records(self.bundle, self.states)
        self.assertEqual(len(result['records']), 2)
        self.assertFalse(result['partial'])
        row = next(r for r in result['records'] if r['record_id'] == self.pending)
        self.assertEqual(row['amount'], '20.00')
        self.assertEqual(row['signed_amount'], '20.00')
        self.assertEqual(row['review_status'], 'confirmed')
        self.assertEqual(ReviewStore(self.bundle, self.states).snapshot()['revision'], 1)
        self.assertEqual(self.hashes, {str(p.relative_to(self.bundle)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.bundle.rglob('*') if p.is_file()})

    def test_validation_does_not_write_on_error(self):
        for fields in ({'amount': '-3'}, {'amount': 'NaN'}, {'amount': '1', 'currency': None},
                       {'amount': '1', 'direction': 'maybe'}, {'amount': '1', 'transaction_at': '2026-99-99'},
                       {'amount': '1', 'balance': 'xx'}, {'amount': '1', 'record_id': 'overwrite'},
                       {'amount': '1', 'transaction_status': 'guessed'}):
            with self.subTest(fields=fields), self.assertRaises(ReviewError): self.confirm(**fields)
        self.assertEqual(self.store.snapshot()['revision'], 0)

    def test_unknown_optional_balance_can_stay_unknown(self):
        self.confirm(balance=None, amount='1.10')
        self.assertIsNone(self.store.detail(self.pending)['record']['balance'])

    def test_zero_is_not_missing(self):
        self.confirm(amount='0', direction='none')
        self.assertEqual(self.store.detail(self.pending)['record']['amount'], '0')

    def test_correction_updates_sign_and_precision(self):
        self.confirm(self.ready, amount='9.90', direction='in', transaction_at='2026-01-02 12:30')
        row = self.store.detail(self.ready)['record']
        self.assertEqual(row['signed_amount'], '9.90')
        self.assertEqual(row['transaction_at_precision'], 'minute')
        self.confirm(self.ready, amount='8.90', transaction_at=row['transaction_at'])
        self.assertEqual(self.store.detail(self.ready)['record']['transaction_at_precision'], 'minute')

    def test_unknown_currency_not_marked_standard(self):
        with self.assertRaises(ReviewError): self.confirm(amount='1', currency='XYZ')

    def test_legacy_bundle_readable(self):
        with closing(sqlite3.connect(self.bundle / 'ledger.sqlite3')) as con, con:
            con.execute('DROP TABLE reviews')
            con.execute('DROP TABLE assets')
            con.execute("UPDATE manifest SET value='\"1.0\"' WHERE key='schema_version'")
        legacy = ReviewStore(self.bundle, self.states)
        self.assertEqual(legacy.snapshot()['summary']['total'], 2)
        self.assertEqual(len(legacy.snapshot()['tasks']), 1)

    def test_evidence_tamper_blocks_review_and_export(self):
        data = b'synthetic-image-placeholder'
        name = 'evidence/test/page.png'
        self.dataset['assets'] = [{'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'role': 'original_page'}]
        self.dataset['_assets'] = {name: data}
        bundle = save_bundle(self.dataset, self.root / 'evidence-bundle', excel=False)
        store = ReviewStore(bundle, self.states)
        self.assertEqual(store.snapshot()['revision'], 0)
        (bundle / name).write_bytes(b'tampered synthetic image')
        with self.assertRaises(ValueError): store.snapshot()
        with self.assertRaises(ValueError): load_effective_records(bundle, self.states, allow_partial=True)

    def test_idempotency_and_conflicting_request(self):
        cmd = self.command(action='confirm', record_id=self.pending, fields={'amount': '1'})
        self.store.apply(cmd)
        self.assertTrue(self.store.apply(cmd)['replayed'])
        with self.assertRaises(Conflict): self.store.apply({**cmd, 'note': 'changed'})
        self.assertEqual(len(self.store.snapshot()['history']), 1)

    def test_revision_conflict(self):
        stale = self.command(action='confirm', record_id=self.pending, fields={'amount': '1'})
        self.confirm()
        with self.assertRaises(Conflict): self.store.apply(stale)

    def test_other_workspace_rejected(self):
        with self.assertRaises(Conflict):
            self.store.apply(self.command(action='confirm', record_id=self.pending, workspace_id='other'))

    def test_concurrent_changes_only_one_commits(self):
        cmds = [self.command(action='confirm', record_id=self.pending, fields={'amount': str(n)}) for n in [1, 2]]
        def run(cmd):
            try: self.store.apply(cmd); return 'ok'
            except Conflict: return 'conflict'
        with ThreadPoolExecutor(2) as pool: results = list(pool.map(run, cmds))
        self.assertCountEqual(results, ['ok', 'conflict'])

    def test_exclude_not_delete_and_undo(self):
        self.store.apply(self.command(action='exclude', record_id=self.pending))
        result = load_effective_records(self.bundle, self.states)
        self.assertEqual(result['excluded_record_ids'], [self.pending])
        self.assertEqual(len(result['records']), 1)
        self.store.apply(self.command(action='undo', target_revision=1))
        self.assertEqual(self.store.snapshot()['summary']['pending'], 1)
        self.assertEqual(len(self.store.snapshot()['history']), 2)

    def test_undo_stack_restores_previous_changes(self):
        self.confirm(amount='1')
        self.confirm(amount='2')
        self.store.apply(self.command(action='undo', target_revision=2))
        self.assertEqual(self.store.detail(self.pending)['record']['amount'], '1')
        self.store.apply(self.command(action='undo', target_revision=1))
        self.assertEqual(self.store.snapshot()['summary']['pending'], 1)
        with self.assertRaises(Conflict): self.store.apply(self.command(action='undo', target_revision=1))

    def test_add_requires_source_and_can_undo(self):
        fields = {'transaction_at': '2026-01-04', 'amount': '3', 'direction': 'out', 'currency': 'CNY'}
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='add', file_id='other', fields=fields))
        self.store.apply(self.command(action='add', file_id=self.dataset['files'][0]['file_id'], fields=fields))
        snap = self.store.snapshot()
        row = next(r for r in snap['records'] if r.get('extraction_method') == 'manual')
        self.assertEqual(row['source_note'], '合成数据测试核验')
        self.assertEqual(snap['summary']['manual'], 1)
        self.store.apply(self.command(action='undo', target_revision=1))
        self.assertEqual(self.store.snapshot()['summary']['total'], 2)

    def test_raw_detail_and_audit_append_only(self):
        self.confirm()
        self.assertEqual(self.store.detail(self.pending)['raw']['values'][1], 'xx')
        with closing(sqlite3.connect(self.store.db)) as con, con:
            with self.assertRaises(sqlite3.IntegrityError): con.execute('DELETE FROM events')
            with self.assertRaises(sqlite3.IntegrityError): con.execute("UPDATE events SET command='{}'")

    def test_source_change_detected(self):
        with closing(sqlite3.connect(self.bundle / 'ledger.sqlite3')) as con, con:
            con.execute("UPDATE manifest SET value='\"changed\"' WHERE key='batch_id'")
        with self.assertRaises(ReviewError): self.store.snapshot()

    def test_state_inside_bundle_rejected(self):
        with self.assertRaises(ReviewError): ReviewStore(self.bundle, self.bundle / 'revisions')

    def test_missing_note_rejected(self):
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='exclude', record_id=self.pending, note=''))

    def test_unscoped_file_error_not_silently_cleared(self):
        self.dataset['issues'].append(issue('HEADER_UNRECOGNIZED', '合成未知表头', file_id=self.dataset['files'][0]['file_id']))
        second = save_bundle(self.dataset, self.root / 'second', excel=False)
        self.store = ReviewStore(second, self.states)
        self.confirm()
        with self.assertRaises(ReviewError): load_effective_records(second, self.states)


class PageTests(unittest.TestCase):
    setUp = StoreTests.setUp
    tearDown = StoreTests.tearDown
    command = StoreTests.command
    confirm = StoreTests.confirm

    def page_setup(self, empty=False):
        data = copy.deepcopy(self.dataset)
        fid = data['files'][0]['file_id']
        data['files'][0]['format'] = 'pdf'
        for row in data['records']: row['page'] = 1
        data['reviews'] = [{'review_id': 'page-task', 'scope': 'page', 'file_id': fid, 'page': 1,
                            'record_id': None, 'status': 'pending', 'evidence': {}, 'reason_codes': ['OCR_NO_ROWS']}]
        data['issues'].append(issue('OCR_PAGE_REVIEW', '整页待核对', file_id=fid, page=1))
        if empty:
            data['records'] = []
            data['issues'] = [issue('OCR_WORKER_FAILED', '失败', file_id=fid, page=1), issue('NO_TRANSACTIONS', '无记录', file_id=fid)]
        bundle = save_bundle(data, self.root / 'page', excel=False)
        self.store = ReviewStore(bundle, self.states)
        return fid, bundle

    def test_page_must_resolve_rows_and_counts(self):
        fid, bundle = self.page_setup()
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=2))
        self.confirm()
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=3))
        self.assertEqual(self.store.snapshot()['summary']['eligible'], 0)
        self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=2))
        self.assertEqual(len(load_effective_records(bundle, self.states)['records']), 2)
        self.confirm(amount='4')
        self.assertEqual(self.store.snapshot()['summary']['page_pending'], 1)

    def test_empty_page_add_and_resolve(self):
        fid, bundle = self.page_setup(empty=True)
        fields = {'transaction_at': '2026-01-04', 'amount': '3', 'direction': 'out', 'currency': 'CNY'}
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='add', file_id=fid, fields=fields))
        self.store.apply(self.command(action='add', file_id=fid, page=1, fields=fields))
        self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=1))
        self.assertFalse(load_effective_records(bundle, self.states)['partial'])

    def test_add_invalidates_page_and_rejects_nonexistent_image_page(self):
        fid, bundle = self.page_setup(empty=True)
        self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=0))
        fields = {'transaction_at': '2026-01-04', 'amount': '3', 'direction': 'out', 'currency': 'CNY'}
        self.store.apply(self.command(action='add', file_id=fid, page=1, fields=fields))
        self.assertEqual(self.store.snapshot()['summary']['page_pending'], 1)
        self.store.files[0]['format'] = 'png'
        with self.assertRaises(ReviewError): self.store.apply(self.command(action='add', file_id=fid, page=2, fields=fields))

    def test_explicit_nontransaction_page(self):
        fid, bundle = self.page_setup(empty=True)
        self.store.apply(self.command(action='resolve_page', review_id='page-task', observed_count=0, note='原页仅为说明，人工确认无交易'))
        self.assertEqual(load_effective_records(bundle, self.states)['records'], [])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        bundle = save_bundle(sample(self.root), self.root / 'bundle', excel=False)
        self.app = App(self.root / 'app', bundle, ocr=False)
        self.server = Server(self.app)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None, auth=True):
        if isinstance(body,dict): body={**self.app.catalog.context(),**body}
        if path.startswith(('/api/detail?','/api/evidence?','/api/export?')):
            from urllib.parse import urlencode
            path += '&'+urlencode(self.app.catalog.context())
        h = {'Authorization': 'Bearer ' + self.app.token} if auth else {}
        if body is not None: h['Content-Type'] = 'application/json'
        h.update(headers or {})
        con = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        con.request('POST' if body is not None else 'GET', path, json.dumps(body) if body is not None else None, h)
        response = con.getresponse(); data = response.read(); status = response.status; rh = dict(response.getheaders()); con.close()
        return status, data, rh

    def test_auth_host_origin_and_no_cache(self):
        self.assertEqual(self.request('/api/state', auth=False)[0], 401)
        self.assertEqual(self.request('/api/state', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('/api/state', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        status, data, headers = self.request('/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_static_shell_has_no_ledger_and_no_path_traversal(self):
        status, data, _ = self.request('/', auth=False)
        self.assertEqual(status, 200)
        self.assertNotIn(b'synthetic.csv', data)
        self.assertEqual(self.request('/../../AGENTS.md')[0], 404)
        self.assertEqual(self.request('/api/evidence?workspace_id=' + self.app.store.workspace_id + '&path=../../AGENTS.md')[0], 400)

    def test_read_export_blocks_partial_and_is_explicit(self):
        path = '/api/export?workspace_id=' + self.app.store.workspace_id
        self.assertEqual(self.request(path)[0], 400)
        status, data, _ = self.request(path + '&allow_partial=true')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)['partial'])

    def test_action_and_stale_workspace(self):
        snap = self.app.store.snapshot()
        cmd = {'action': 'exclude', 'record_id': snap['tasks'][0]['record_id'], 'note': '非交易行', 'workspace_id': snap['workspace_id'], 'request_id': str(uuid.uuid4()), 'expected_revision': 0}
        self.assertEqual(self.request('/api/action', cmd)[0], 200)
        self.assertEqual(self.request('/api/action', {**cmd, 'workspace_id': 'other'})[0], 409)
        self.assertEqual(self.request('/api/action', cmd, auth=False)[0], 401)

    def test_import_and_duplicate_files(self):
        self.app.manage({**self.app.catalog.context(),'action':'create','name':'新导入'})
        import base64
        data = base64.b64encode('交易时间,金额,收支,币种\n2026-01-01,1,支出,人民币\n'.encode()).decode()
        status, _, _ = self.request('/api/import', {'files': [{'name': 'a.csv', 'data': data}, {'name': 'b.csv', 'data': data}]})
        self.assertEqual(status, 202)
        deadline = time.monotonic() + 10
        while self.app.job['status'] == 'running' and time.monotonic() < deadline: time.sleep(.02)
        self.assertEqual(self.app.job['status'], 'completed')
        snap = self.app.store.snapshot()
        self.assertEqual(snap['summary']['total'], 1)
        self.assertEqual(snap['files'][1]['status'], 'duplicate_file_skipped')
        self.assertEqual(list((self.app.root / 'temporary-uploads').iterdir()), [])
        resumed = App(self.app.root, ocr=False)
        self.assertEqual(resumed.store.workspace_id, self.app.store.workspace_id)

    def test_invalid_upload_names_and_empty_data(self):
        for name in ['../a.csv', 'C:\\a.csv', 'CON.csv', 'a.csv.', 'a/b.csv', 'x\x00.csv']:
            with self.subTest(name=name), self.assertRaises(ReviewError): safe_filename(name)
        self.assertEqual(self.request('/api/import', {'files': [{'name': 'a.csv', 'data': ''}]})[0], 400)

    def test_write_content_type_and_size_limits(self):
        self.assertEqual(self.request('/api/action', {}, headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.request('/api/action', {'oversize': 'a' * 70000})[0], 400)
        self.assertEqual(self.request('/api/import', {'files': []})[0], 400)

    def test_restart_restores_revision(self):
        snap = self.app.store.snapshot()
        self.app.store.apply({'workspace_id': snap['workspace_id'], 'expected_revision': 0, 'request_id': str(uuid.uuid4()),
                              'action': 'exclude', 'record_id': snap['tasks'][0]['record_id'], 'note': '合成错误行排除'})
        resumed = App(self.app.root, ocr=False)
        self.assertEqual(resumed.store.snapshot()['revision'], 1)
        self.assertEqual(resumed.store.snapshot()['summary']['excluded'], 1)

    def test_books_api_requires_auth_and_rejects_stale_context(self):
        old=self.app.catalog.context()
        snap=self.app.store.snapshot()
        status,data,_=self.request('/api/books',{'action':'create','name':'另一账本'})
        self.assertEqual(status,200)
        self.assertIsNone(json.loads(data)['ledger'])
        self.assertEqual(self.request('/api/books',{'action':'rename','target_id':old['book_id'],'name':'旧请求',**old})[0],409)
        self.assertEqual(self.request('/api/books',{'action':'create','name':'未授权'},auth=False)[0],401)
        # Even after switching back to the same source, the previous page cannot write.
        self.assertEqual(self.request('/api/books',{'action':'open','target_id':old['book_id']})[0],200)
        cmd={'action':'exclude','record_id':snap['tasks'][0]['record_id'],'note':'旧页面',
             'workspace_id':snap['workspace_id'],'expected_revision':0,'request_id':uuid.uuid4().hex,**old}
        self.assertEqual(self.request('/api/action',cmd)[0],409)
        self.assertEqual(self.app.store.snapshot()['revision'],0)


if __name__ == '__main__': unittest.main()
