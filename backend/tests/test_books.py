from __future__ import annotations
import base64
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.server import App, Server
from jiaowopay_review.catalog import Catalog
from jiaowopay_review.store import ReviewStore, ReviewError, Conflict
from jiaowopay_review.lock import DataDirectoryLock
from jiaowopay_ingest import import_files, save_bundle


class BooksTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.app=App(self.root/'app',ocr=False)

    def tearDown(self):
        self.tmp.cleanup()

    def manage(self,action,bid=None,**kw):
        return self.app.manage({**self.app.catalog.context(),'action':action,'target_id':bid,**kw})

    def create(self,name='测试账本'):
        return self.manage('create',name=name)['book_id']

    def import_csv(self):
        data=base64.b64encode('交易日期,金额,收支\n2026-01-01,xx,支出\n'.encode()).decode()
        self.app.start_import({**self.app.catalog.context(),'files':[{'name':'合成.csv','data':data}]})
        deadline=time.monotonic()+5
        while self.app.job['status']=='running' and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.app.job['status'],'completed')

    def apply(self,**kw):
        store=self.app.store
        return store.apply({'workspace_id':store.workspace_id,'expected_revision':store.snapshot()['revision'],
                            'request_id':uuid.uuid4().hex,'action':'exclude','record_id':next(iter(store.originals)),
                            'note':'合成测试非交易行',**kw})

    def test_empty_create_rename_restart(self):
        bid=self.create('生活费')
        self.assertIsNone(self.app.store)
        self.manage('rename',bid,name='九月生活费')
        resumed=App(self.app.root,ocr=False)
        self.assertEqual(resumed.catalog.context()['book_id'],bid)
        self.assertEqual(resumed.snapshot()['books'][0]['name'],'九月生活费')
        self.assertIsNone(resumed.store)

    def test_import_switch_and_revision_isolation(self):
        one=self.create('A');self.import_csv();self.apply()
        first=self.app.store.db
        two=self.create('B');self.import_csv()
        self.assertNotEqual(first,self.app.store.db)
        self.assertEqual(self.app.store.snapshot()['revision'],0)
        self.manage('open',one)
        self.assertEqual(self.app.store.snapshot()['revision'],1)
        self.manage('open',two)
        self.assertEqual(self.app.store.snapshot()['summary']['excluded'],0)

    def test_existing_book_rejects_duplicate_file_by_default(self):
        self.create();self.import_csv()
        with self.assertRaises(Conflict): self.import_csv()

    def test_stale_context_after_switch_away_and_back(self):
        one=self.create('A');old=self.app.catalog.context()
        self.create('B');self.manage('open',one)
        with self.assertRaises(Conflict): self.app.manage({**old,'action':'rename','target_id':one,'name':'wrong'})

    def test_reset_is_atomic_new_empty_and_recoverable(self):
        old=self.create('实验');self.import_csv();self.apply()
        snap=self.manage('reset',old)
        self.assertNotEqual(snap['book_id'],old)
        self.assertIsNone(snap['ledger'])
        self.assertTrue(self.app.catalog.get(old)['deleted'])
        self.manage('restore',old);self.manage('open',old)
        self.assertEqual(self.app.store.snapshot()['revision'],1)

    def test_trash_active_restart_and_restore(self):
        bid=self.create();self.import_csv()
        self.manage('trash',bid)
        self.assertIsNone(self.app.catalog.context()['book_id'])
        self.app=App(self.app.root,ocr=False)
        self.assertIsNone(self.app.store)
        self.manage('restore',bid);self.manage('open',bid)
        self.assertEqual(self.app.store.snapshot()['summary']['total'],1)

    def test_purge_requires_name_and_trash_and_only_deletes_target(self):
        a=self.create('A');self.import_csv();folder=self.app.root/'books'/a
        other=self.create('B');self.import_csv()
        with self.assertRaises(ReviewError): self.manage('purge',a,confirmation='A')
        self.manage('trash',a)
        with self.assertRaises(ReviewError): self.manage('purge',a,confirmation='wrong')
        self.manage('purge',a,confirmation='A')
        self.assertFalse(folder.exists())
        self.assertTrue((self.app.root/'books'/other).exists())
        self.assertEqual(self.app.store.snapshot()['summary']['total'],1)

    def test_purge_empty(self):
        bid=self.create();self.manage('trash',bid)
        self.manage('purge',bid,confirmation='测试账本')
        self.assertEqual(self.app.snapshot()['books'],[])

    def test_unknown_id_and_bad_names(self):
        for name in ['', ' '*2, 'a'*81, 'x\nY', None]:
            with self.assertRaises(ReviewError): self.create(name)
        with self.assertRaises(ReviewError): self.manage('open','../../outside')
        self.assertEqual(len(self.app.snapshot()['books']),0)

    def test_management_blocked_during_import(self):
        bid=self.create();self.app.job={'status':'running'}
        for action in ['create','open','rename','trash','reset']:
            with self.assertRaises(Conflict): self.manage(action,bid,name='later')
        self.app.job={'status':'idle'}

    def test_failed_import_keeps_empty_book_and_other_books(self):
        a=self.create('A');self.import_csv();b=self.create('B')
        with patch('app.service.import_files',side_effect=RuntimeError('synthetic')):
            self.app._import([('a.csv',b'abc')],'fakejob',b)
        self.assertEqual(self.app.job['status'],'failed')
        self.assertIsNone(self.app.catalog.get(b)['bundle'])
        self.manage('open',a)
        self.assertEqual(self.app.store.snapshot()['summary']['total'],1)

    def legacy(self):
        csv=self.root/'old.csv';csv.write_text('交易日期,金额,收支\n2026-01-01,xx,支出\n',encoding='utf-8')
        bundle=save_bundle(import_files([csv]),self.app.root/'imports'/'old',excel=False)
        store=ReviewStore(bundle,self.app.root/'revisions')
        store.apply({'workspace_id':store.workspace_id,'expected_revision':0,'request_id':uuid.uuid4().hex,'action':'exclude',
                     'record_id':next(iter(store.originals)),'note':'原有修订保留'})
        (self.app.root/'last-bundle.json').write_text(json.dumps({'bundle':str(bundle)}),encoding='utf-8')
        return bundle,store

    def test_legacy_migration_is_idempotent_and_preserves_history(self):
        bundle,store=self.legacy()
        # Simulate the first launch of a legacy directory (no previous catalog).
        self.app.catalog.db.unlink()
        self.app=App(self.app.root,ocr=False)
        self.assertEqual(len(self.app.snapshot()['books']),1)
        self.assertEqual(self.app.store.snapshot()['revision'],1)
        bid=self.app.catalog.context()['book_id']
        self.manage('rename',bid,name='保留名字')
        resumed=App(self.app.root,ocr=False)
        self.assertEqual(len(resumed.snapshot()['books']),1)
        self.assertEqual(resumed.snapshot()['books'][0]['name'],'保留名字')

    def test_identical_legacy_bundles_do_not_share_new_revisions(self):
        bundle,store=self.legacy();copy=self.app.root/'imports'/'copy'
        shutil.copytree(bundle,copy)
        self.app=App(self.app.root,ocr=False)
        books=self.app.catalog.rows()
        self.assertEqual(len(books),2)
        self.assertNotEqual(books[0]['states'],books[1]['states'])
        self.manage('open',books[0]['id'])
        self.apply(action='undo',target_revision=1)
        self.manage('open',books[1]['id'])
        self.assertEqual(self.app.store.snapshot()['revision'],1)

    def test_purged_legacy_is_not_resurrected(self):
        bundle,store=self.legacy();self.app=App(self.app.root,ocr=False)
        bid=self.app.catalog.rows()[0]['id']
        name=self.app.catalog.get(bid)['name']
        self.manage('trash',bid);self.manage('purge',bid,confirmation=name)
        self.assertFalse(bundle.exists())
        self.assertFalse(store.db.exists())
        self.assertEqual(App(self.app.root,ocr=False).snapshot()['books'],[])

    def test_external_source_never_deleted(self):
        bundle,store=self.legacy();external=self.root/'outside';shutil.copytree(bundle,external)
        self.app=App(self.root/'other-app',external,ocr=False)
        bid=self.app.catalog.context()['book_id'];name=self.app.catalog.get(bid)['name']
        self.manage('trash',bid);self.manage('purge',bid,confirmation=name)
        self.assertTrue((external/'ledger.sqlite3').exists())

    def test_delete_path_tampering_rejected(self):
        bid=self.create();self.import_csv();self.manage('trash',bid)
        with closing(sqlite3.connect(self.app.catalog.db)) as con, con:
            con.execute('UPDATE books SET bundle=? WHERE id=?',(str(self.root/'outside'),bid))
        with self.assertRaises(ReviewError): self.manage('purge',bid,confirmation='测试账本')

    def test_corrupt_active_book_does_not_hide_manager(self):
        bid=self.create();self.import_csv()
        self.app.store.source_db.write_bytes(b'corrupt-test-database')
        snap=self.app.snapshot()
        self.assertIsNone(snap['ledger'])
        self.assertTrue(snap['open_error'])
        self.assertEqual(snap['books'][0]['id'],bid)
        self.create('可继续管理')

    def test_data_directory_lock(self):
        lock=DataDirectoryLock(self.app.root)
        try:
            with self.assertRaises(ReviewError): DataDirectoryLock(self.app.root)
        finally: lock.close()
        lock=DataDirectoryLock(self.app.root);lock.close()


if __name__=='__main__': unittest.main()
