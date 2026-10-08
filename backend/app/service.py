"""Local import/review service, independent of reconciliation and external AI."""
from __future__ import annotations
import base64
import binascii
import hashlib
import json
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from jiaowopay_ingest import import_files, save_bundle
from jiaowopay_review.store import ReviewStore, ReviewError, Conflict
from jiaowopay_review.catalog import Catalog, name_value
from jiaowopay_review.bookstore import book_snapshot, export_book
from .views import overview, records_page

MAX_UPLOAD = 50 * 1024 * 1024

def safe_filename(name):
    if (not isinstance(name, str) or not name or len(name) > 160
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.'))
            or name in {'.', '..'} or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?', name, re.I)):
        raise ReviewError('文件名包含不支持的字符，请重命名后导入。')
    return name

class App:
    def __init__(self, root, bundle=None, *, ocr=True, ocr_python=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.catalog = Catalog(self.root)
        self.catalog.migrate(bundle)
        self.ocr, self.ocr_python = ocr, ocr_python
        self.job = {'status': 'idle'}
        self.job_path = self.root / 'import-task.json'
        self.recover_import()
        self.sync_book()

    def save_job(self, job):
        temporary = self.job_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.job_path)
        self.job = job

    def recover_import(self):
        if not self.job_path.exists():
            return
        try:
            job = json.loads(self.job_path.read_text(encoding='utf-8'))
            if not isinstance(job, dict):
                raise ValueError()
            self.job = job
            if job.get('status') not in {'running', 'interrupted'}:
                return
            bid, jid = job.get('book_id', ''), job.get('id', '')
            if not re.fullmatch('[a-f0-9]{32}', bid) or not re.fullmatch('[a-f0-9]{32}', jid):
                raise ValueError()
            bundle = self.root / 'books' / bid / ('import-' + jid)
            if (bundle / 'COMPLETE').is_file():
                batches = self.catalog.batches(bid)
                if not any(Path(b['bundle']).resolve() == bundle.resolve() for b in batches):
                    self.catalog.attach(bid, bundle, job.get('batch_name'))
                self.save_job({**job, 'status': 'completed', 'message': '已恢复完整保存的导入批次，请查看读取结果。'})
            else:
                self.save_job({**job, 'status': 'interrupted', 'message': '上次导入中断，未完成内容未计入账本。请重新选择文件导入，旧批次和修订仍保留。'})
        except Exception:
            self.job = {'status': 'interrupted', 'message': '上次导入状态无法恢复。原数据保留，请查看批次后再重试。'}

    def overview(self):
        return overview(self.catalog)

    def records(self, params):
        return records_page(self.catalog, params)

    def apply(self, payload):
        with self.lock:
            if self.job['status'] == 'running':
                raise Conflict('导入期间暂停修改；请等待任务完成。')
            self.catalog.check(payload)
            result = self.current(batch_id=payload.get('batch_id')).apply(payload)
            self.catalog.touch(payload['book_id'])
            return result

    def sync_book(self):
        self.store = None
        self.states = None
        self.open_error = None
        self.batch_id = None
        bid = self.catalog.context()['book_id']
        if bid:
            book = self.catalog.get(bid)
            if book['deleted']:
                return
            batch=self.catalog.selected_batch(bid)
            if batch:
                self.batch_id=batch['id']
                self.states = Path(batch['states'])
                try:
                    self.store = ReviewStore(batch['bundle'], self.states)
                    self.store.snapshot()
                except Exception:
                    self.store = None
                    self.open_error = '当前账本无法读取，数据仍保留；请检查底稿和证据，也可切换其他账本。'

    def snapshot(self):
        ledger = None
        if self.store:
            try:
                ledger = self.store.snapshot()
            except Exception:
                self.open_error = '当前账本底稿或证据发生变化，已暂停核验；可切换其他账本，原文件未删除。'
        books=[]
        for book in self.catalog.rows():
            item={k:book[k] for k in ['id','name','created','updated','deleted']}
            item['has_data']=bool(book['bundle'])
            item['external_source']=not book['owned']
            item.update(book_snapshot(self.catalog,book['id']))
            item['batch_count']=len(item['batches'])
            if item['unreadable_batches']:
                item['error']='部分批次无法读取，已知数量不包含这些批次；请查看批次列表。'
            books.append(item)
        return {**self.catalog.context(),'books':books,'warnings':self.catalog.warnings,
                'open_error':self.open_error,'job':self.job,
                'ledger':ledger,'batch_id':self.batch_id,
                'bundle':str(self.store.bundle) if self.store else None}

    def manage(self,payload):
        with self.lock:
            self.catalog.check(payload)
            if self.job['status']=='running':
                raise Conflict('正在导入，请等待完成后再管理或切换账本。')
            action=payload.get('action')
            bid=payload.get('target_id')
            if action=='create': self.catalog.create(payload.get('name'))
            elif action=='open': self.catalog.select(bid)
            elif action=='open_batch': self.catalog.select_batch(self.catalog.context()['book_id'],payload.get('batch_id'))
            elif action=='purge': self.catalog.purge(bid,payload.get('confirmation'))
            else: self.catalog.change(action,bid,payload.get('name'))
            self.sync_book()
            # Retain the last task status across book navigation.
            return self.snapshot()

    def current(self, workspace_id=None, batch_id=None):
        if not self.store:
            raise ReviewError('请先导入账单，或启动时指定已有数据包。')
        if workspace_id is not None and self.store.workspace_id != workspace_id:
            raise Conflict('账本已切换，请刷新后重试。')
        if batch_id is not None and batch_id != self.batch_id:
            raise Conflict('批次已切换，请刷新后重试。')
        return self.store

    def start_import(self, payload):
        if not isinstance(payload, dict):
            raise ReviewError('导入请求无效。')
        files = payload.get('files')
        batch_name=payload.get('batch_name')
        if batch_name is not None and batch_name != '': batch_name=name_value(batch_name)
        allow_duplicates=payload.get('allow_duplicate_files',False)
        if type(allow_duplicates) is not bool: raise ReviewError('重复文件选项无效。')
        if not isinstance(files, list) or not 1 <= len(files) <= 20:
            raise ReviewError('每批请选择 1–20 个文件。')
        decoded, total = [], 0
        for item in files:
            if not isinstance(item, dict):
                raise ReviewError('文件参数无效。')
            name = safe_filename(item.get('name'))
            try:
                data = base64.b64decode(item['data'], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error):
                raise ReviewError('文件传输不完整。') from None
            total += len(data)
            if not data or total > MAX_UPLOAD:
                raise ReviewError('单批文件总量必须为 1 字节至 50 MiB。较大账单请使用命令入口。')
            decoded.append((name, data))
        with self.lock:
            self.catalog.check(payload)
            if self.job['status'] == 'running':
                raise Conflict('已有导入任务，请等待完成。')
            book_id=self.catalog.context()['book_id']
            if not book_id:
                raise ReviewError('请先新建或打开账本。')
            if not allow_duplicates:
                known=set()
                for batch in self.catalog.batches(book_id):
                    try:
                        store=ReviewStore(batch['bundle'],batch['states'])
                        known.update(f['sha256'] for f in store.files if f.get('sha256'))
                    except Exception:
                        raise ReviewError('已有批次无法读取，暂不能检查重复文件；请修复数据包或明确勾选允许重复文件。') from None
                duplicate=[name for name,data in decoded if hashlib.sha256(data).hexdigest() in known]
                if duplicate:
                    raise Conflict('以下文件内容已在本账本中存在：'+ '、'.join(duplicate)+ '。本批尚未导入，请移除重复文件；需要重新识别时可勾选允许重复文件，两批将同时保留。')
            job_id = uuid.uuid4().hex
            self.save_job({'status': 'running', 'id': job_id, 'book_id': book_id, 'batch_name': batch_name,
                           'started_at': time.time(), 'progress': {'stage': 'preparing', 'completed': 0, 'total': len(decoded)},
                           'message': '正在本机读取；扫描页可能需要数分钟，请勿关闭服务。'})
            # Non-daemon: a normal stop must not tear down a half-written import.
            thread = threading.Thread(target=self._import, args=(decoded, job_id, book_id, batch_name), daemon=False)
            thread.start()
            return dict(self.job)

    def _import(self, files, job_id, book_id, batch_name=None):
        bundle = None
        def report(progress):
            with self.lock:
                self.save_job({**self.job, 'progress': progress})
        try:
            incoming = self.root / 'temporary-uploads'
            incoming.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='import-', dir=incoming) as work:
                paths = []
                for i, (name, data) in enumerate(files):
                    folder = Path(work) / str(i)
                    folder.mkdir()
                    path = folder / name
                    path.write_bytes(data)
                    paths.append(path)
                dataset = import_files(paths, ocr=self.ocr, ocr_python=self.ocr_python, progress=report)
                report({'stage': 'saving', 'completed': len(files), 'total': len(files)})
                bundle = save_bundle(dataset, self.root / 'books' / book_id / ('import-' + job_id), excel=False)
            with self.lock:
                self.catalog.attach(book_id,bundle,batch_name)
                self.sync_book()
                self.save_job({**self.job, 'status': 'completed', 'finished_at': time.time(),
                               'message': '读取完成，请检查待核验项与文件异常。'})
        except Exception:
            # Never return raw exceptions containing statement values or filesystem details.
            with self.lock:
                saved = bundle is not None and (bundle / 'COMPLETE').is_file()
                self.save_job({**self.job, 'status': 'interrupted' if saved else 'failed', 'id': job_id,
                               'book_id': book_id, 'batch_name': batch_name,
                               'message': '批次已完整保存但登记未完成。重启应用将尝试恢复；旧批次保留。' if saved else
                               '新批次导入未完成，之前的批次和核验进度保留。请检查文件与本地依赖后重试，不完整结果不作为账本使用。'})
