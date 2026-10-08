import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from app.server import App
from jiaowopay_review.bookstore import export_book
from jiaowopay_review.store import ReviewStore
from jiaowopay_ingest import import_files,save_bundle


class PortabilityTests(unittest.TestCase):
    def test_move_book_with_two_batches_and_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); old=root/'原位置'; app=App(old,ocr=False)
            bid=app.catalog.create('迁移核验')
            for index in range(2):
                csv=root/f'{index}.csv';csv.write_text(f'交易日期,金额,收支\n2026-01-01,{index+1},支出\n',encoding='utf-8')
                bundle=save_bundle(import_files([csv],ocr=False),old/'books'/bid/f'import-{index}',excel=False)
                app.catalog.attach(bid,bundle)
            app.sync_book();batch=app.batch_id
            app.store.apply({'workspace_id':app.store.workspace_id,'expected_revision':0,'request_id':uuid.uuid4().hex,
                             'action':'exclude','record_id':next(iter(app.store.originals)),'note':'迁移合成测试'})
            before=export_book(app.catalog,bid)
            new=root/'另一处含 空格'/'数据';shutil.copytree(old,new)
            shutil.rmtree(old)  # Temporary fixture: old source is unavailable.
            moved=App(new,ocr=False)
            self.assertEqual(moved.batch_id,batch)
            after=export_book(moved.catalog,bid)
            for key in ('records','batches','summary','excluded_record_ids'):
                self.assertEqual(before[key],after[key])
            for b in moved.catalog.batches(bid):
                self.assertTrue(Path(b['bundle']).is_relative_to(new))
                self.assertTrue(Path(b['states']).is_relative_to(new))
            self.assertEqual(App(new,ocr=False).catalog.context(),moved.catalog.context())

    def test_moved_legacy_pointer_does_not_reregister_or_drop_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=root/'old';csv=root/'sample.csv'
            csv.write_text('交易日期,金额,收支\n2026-01-01,4,支出\n',encoding='utf-8')
            bundle=save_bundle(import_files([csv],ocr=False),old/'imports'/'legacy',excel=False)
            (old/'last-bundle.json').write_text(json.dumps({'bundle':str(bundle)}),encoding='utf-8')
            app=App(old,ocr=False);bid=app.catalog.context()['book_id']
            new=root/'new';shutil.copytree(old,new);shutil.rmtree(old)
            moved=App(new,ocr=False)
            self.assertEqual(len(moved.catalog.rows()),1)
            self.assertEqual(moved.catalog.context()['book_id'],bid)
            self.assertEqual(moved.store.snapshot()['summary']['eligible'],1)
            self.assertTrue(Path(json.loads((new/'last-bundle.json').read_text(encoding='utf-8'))['bundle']).is_relative_to(new))

    def test_external_bundle_reference_remains_external(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);csv=root/'sample.csv';csv.write_text('交易日期,金额,收支\n2026-01-01,7,支出\n',encoding='utf-8')
            bundle=save_bundle(import_files([csv],ocr=False),root/'external',excel=False)
            old=root/'old';app=App(old,bundle,ocr=False)
            new=root/'new';shutil.copytree(old,new);shutil.rmtree(old)
            moved=App(new,ocr=False)
            self.assertEqual(moved.store.bundle,bundle)
            self.assertTrue(moved.store.state_dir.is_relative_to(new))
