import base64
import json
import sqlite3
import time
from contextlib import closing
from unittest.mock import patch

from test_books import BooksTests
from app.server import App
from jiaowopay_review.bookstore import export_book
from jiaowopay_review.store import ReviewError, Conflict


class MultiBatchTests(BooksTests):
    # Reuse helpers without rerunning inherited tests.
    def append(self, amount='12', name='追加.csv', allow=False, batch_name=''):
        data=base64.b64encode(f'交易日期,金额,收支\n2026-01-02,{amount},支出\n'.encode()).decode()
        self.app.start_import({**self.app.catalog.context(),'batch_name':batch_name,
                              'allow_duplicate_files':allow,'files':[{'name':name,'data':data}]})
        deadline=time.monotonic()+10
        while self.app.job['status']=='running' and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.app.job['status'],'completed')

    def test_append_keeps_history_and_restart_selection(self):
        bid=self.create();self.import_csv();self.apply()
        first=self.app.batch_id; db=self.app.store.db
        self.append(batch_name='十月银行卡');second=self.app.batch_id
        snap=self.app.snapshot()['books'][0]
        self.assertEqual(snap['summary']['total'],2)
        self.assertEqual(snap['summary']['excluded'],1)
        self.assertEqual(snap['batch_count'],2)
        self.app=App(self.app.root,ocr=False)
        self.assertEqual(self.app.batch_id,second)
        self.manage('open_batch',batch_id=first)
        self.assertEqual(self.app.store.db,db)
        self.assertEqual(self.app.store.snapshot()['revision'],1)
        self.assertEqual(len(self.app.catalog.batches(bid)),2)

    def test_export_checks_unselected_pending_batch(self):
        bid=self.create();self.import_csv();self.append()
        self.assertEqual(self.app.store.snapshot()['summary']['pending'],0)
        with self.assertRaises(ReviewError): export_book(self.app.catalog,bid)
        data=export_book(self.app.catalog,bid,allow_partial=True)
        self.assertTrue(data['partial']);self.assertEqual(len(data['records']),1)
        self.assertEqual(len(data['batches']),2)
        self.assertEqual(len(data['pending_record_ids']),1)
        self.assertNotIn('revision',data)

    def test_repeated_bytes_need_explicit_permission_and_isolate_ids(self):
        bid=self.create();self.append();first=self.app.batch_id
        rid=next(iter(self.app.store.originals))
        with self.assertRaises(Conflict): self.append(name='改名.csv')
        self.append(name='改名.csv',allow=True)
        self.assertEqual(next(iter(self.app.store.originals)),rid)
        data=export_book(self.app.catalog,bid)
        self.assertEqual(len(data['records']),2)
        self.assertEqual(len({r['book_record_id'] for r in data['records']}),2)
        self.apply()
        self.manage('open_batch',batch_id=first)
        self.assertEqual(self.app.store.snapshot()['revision'],0)

    def test_same_filename_changed_contents_allowed(self):
        self.create();self.append();self.append(amount='13')
        self.assertEqual(self.app.snapshot()['books'][0]['summary']['eligible'],2)

    def test_failed_append_preserves_existing_and_context(self):
        bid=self.create();self.import_csv();self.apply();ctx=self.app.catalog.context()
        with patch('app.service.save_bundle',side_effect=OSError('synthetic')):
            self.app._import([('fail.csv',b'abc')],'failure',bid)
        self.assertEqual(self.app.job['status'],'failed')
        self.assertEqual(self.app.catalog.context(),ctx)
        self.assertEqual(self.app.store.snapshot()['revision'],1)
        self.assertEqual(len(self.app.catalog.batches(bid)),1)

    def test_switch_batch_rejects_stale_context_and_foreign_batch(self):
        a=self.create();self.append();first=self.app.batch_id;self.append(amount='2')
        ctx=self.app.catalog.context();self.manage('open_batch',batch_id=first)
        with self.assertRaises(Conflict): self.app.catalog.check(ctx)
        self.create('另一账本');self.append();foreign=self.app.batch_id
        self.manage('open',a)
        with self.assertRaises(ReviewError): self.manage('open_batch',batch_id=foreign)

    def test_single_book_schema_migration_preserves_review(self):
        bid=self.create();self.import_csv();self.apply();db=self.app.store.db
        with closing(self.app.catalog.connect()) as con, con:
            con.execute('DROP TABLE batches')
            con.execute("DELETE FROM settings WHERE key LIKE 'batch:%'")
        self.app=App(self.app.root,ocr=False)
        self.assertEqual(self.app.store.db,db)
        self.assertEqual(self.app.store.snapshot()['revision'],1)
        self.append();self.app=App(self.app.root,ocr=False)
        self.assertEqual(len(self.app.catalog.batches(bid)),2)

    def test_corrupt_batch_export_explicit_partial_and_switch(self):
        bid=self.create();self.append();first=self.app.batch_id
        source=self.app.store.source_db;self.append(amount='3');source.write_bytes(b'corrupt')
        snap=self.app.snapshot()['books'][0]
        self.assertEqual(snap['unreadable_batches'],1)
        with self.assertRaises(ReviewError): export_book(self.app.catalog,bid)
        data=export_book(self.app.catalog,bid,allow_partial=True)
        self.assertEqual(data['unreadable_batch_ids'],[first]);self.assertTrue(data['partial'])
        self.assertEqual(len(data['records']),1)
        self.manage('open_batch',batch_id=first)
        self.assertIsNone(self.app.snapshot()['ledger'])
        self.assertEqual(len(self.app.snapshot()['books'][0]['batches']),2)

    def test_reset_restore_and_purge_all_batches_only_target(self):
        old=self.create();self.append();self.append(amount='4')
        self.manage('reset',old);new=self.app.catalog.context()['book_id'];self.append()
        self.manage('restore',old);self.manage('open',old)
        self.assertEqual(self.app.snapshot()['books'][0]['batch_count'],2)
        paths=[b['bundle'] for b in self.app.catalog.batches(old)]
        self.manage('trash',old);self.manage('purge',old,confirmation='测试账本')
        from pathlib import Path
        self.assertTrue(all(not Path(p).exists() for p in paths))
        self.manage('open',new)
        self.assertEqual(self.app.store.snapshot()['summary']['total'],1)

    def test_legacy_append_restart_and_purge(self):
        bundle,store=self.legacy();self.app=App(self.app.root,ocr=False)
        bid=self.app.catalog.rows()[0]['id'];self.manage('open',bid);self.append()
        self.app=App(self.app.root,ocr=False)
        self.assertEqual(len(self.app.catalog.batches(bid)),2)
        self.assertEqual(self.app.snapshot()['books'][0]['summary']['excluded'],1)
        name=self.app.catalog.get(bid)['name']
        self.manage('trash',bid);self.manage('purge',bid,confirmation=name)
        self.assertFalse(bundle.exists());self.assertFalse(store.db.exists())


# unittest also discovers imported TestCase classes; inherit just the fixture helpers.
for name in list(BooksTests.__dict__):
    if name.startswith('test_'): setattr(MultiBatchTests,name,None)
del BooksTests

from test_review import ServerTests


class MultiBatchHTTPTests(ServerTests):
    def test_append_switch_stale_action_and_book_export(self):
        old=self.app.batch_id
        rid=next(iter(self.app.store.originals))
        ctx=self.app.catalog.context()
        data=base64.b64encode('交易日期,金额,收支\n2026-02-01,42,支出\n'.encode()).decode()
        code,_,_=self.request('/api/import',{'files':[{'name':'second.csv','data':data}],'batch_name':'二月'})
        self.assertEqual(code,202)
        deadline=time.monotonic()+10
        while self.app.job['status']=='running' and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.app.job['status'],'completed')
        second=self.app.batch_id
        code,body,_=self.request('/api/state')
        self.assertEqual(json.loads(body)['books'][0]['batch_count'],2)
        self.assertEqual(self.request('/api/export?allow_partial=false')[0],400)
        code,body,_=self.request('/api/export?allow_partial=true')
        result=json.loads(body)
        self.assertEqual(code,200);self.assertEqual(len(result['batches']),2)
        self.assertTrue(any(r['amount']=='42' for r in result['records']))
        code,_,_=self.request('/api/books',{'action':'open_batch','batch_id':old})
        self.assertEqual(code,200)
        self.assertEqual(self.request('/api/detail?workspace_id='+self.app.store.workspace_id+'&record_id='+rid+'&batch_id='+second)[0],409)
        self.assertEqual(self.request('/api/detail?workspace_id='+self.app.store.workspace_id+'&record_id='+rid+'&batch_id='+old)[0],200)


for name in list(ServerTests.__dict__):
    if name.startswith('test_'): setattr(MultiBatchHTTPTests,name,None)
del ServerTests
