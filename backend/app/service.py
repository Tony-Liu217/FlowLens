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
from analysis.store import AnalysisStore
from analysis.ai import PairingAI, MODEL, POLICY as AI_POLICY
from analysis.core import flow_direction, is_refund, rules_need_restart

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
        self.analysis_stores = {}
        self.pairing_ai = PairingAI(self)

    def analysis_store(self, book_id):
        book=self.catalog.get(book_id)
        if book['deleted']:raise ReviewError('账本已移入回收站。')
        if book_id not in self.analysis_stores:
            self.analysis_stores[book_id]=AnalysisStore(self.root/'books'/book_id/'analysis')
        return self.analysis_stores[book_id]

    def analysis_data(self, book_id=None):
        from jiaowopay_review.bookstore import export_book
        bid=book_id or self.catalog.context()['book_id']
        if not bid:raise ReviewError('请先选择账本。')
        return export_book(self.catalog,bid,allow_partial=True)

    def analysis_view(self, params):
        data=self.analysis_data();store=self.analysis_store(data['book_id']);view=store.view(data)
        value=lambda key,default='':params.get(key,[default])[0]
        section=value('section','summary')
        jobs=self.pairing_ai.jobs(store)
        # Results only annotate existing candidates with validated enum values.
        annotations={}
        for job in reversed(jobs):
            if job.get('policy')!=AI_POLICY or job.get('fingerprint')!=view['fingerprint']:continue
            for item in job.get('results',[]):
                annotations[item['relation_id']]=item
        def annotated(p):
            item=annotations.get(p['id'])
            return {**p,'ai_verdict':item['verdict'] if item and item['dependencies']==p['dependencies'] else None}
        def brief(t):
            return {k:t.get(k) for k in ('id','transaction_at','booking_at','counterparty','description','amount','currency','direction','refund','refund_linked','included','source_ids')}
        if section=='export':return {**view,'ai_jobs':[{k:v for k,v in j.items() if k!='results'} for j in jobs]}
        if section=='detail':
            tx=next((t for t in view['transactions'] if t['id']==value('id')),None)
            if not tx:raise ReviewError('交易已变化，请刷新。')
            return {'transaction':tx,'relations':[p for p in view['relations'] if set(p['source_ids'])&set(tx['source_ids'])],
                    'fingerprint':view['fingerprint'],'revision':view['revision']}
        if section=='history':return {'history':view['history'],'fingerprint':view['fingerprint'],'revision':view['revision']}
        if section in {'transactions','candidates'}:
            page=int(value('page','1'));size=int(value('page_size','30'))
            if page<1 or not 1<=size<=100:raise ReviewError('页码或每页数量无效。')
            if section=='transactions':
                q=value('q').casefold();items=[brief(t) for t in view['transactions'] if not q or q in str(t.get('counterparty','')).casefold() or q in str(t.get('description','')).casefold()]
            else:
                items=sorted(view['candidates'],key=lambda p:float(p['amount']),reverse=True)
                if value('show_all')!='true':items=[p for p in items if p['decision'] in {'suggested','stale'}]
                rows={r['book_record_id']:r for r in data['records']}
                def source_label(row):
                    sequence=next((str(f['value']) for f in row.get('source_fields',[]) if f.get('header')=='序号' and f.get('value') not in (None,'')),None)
                    if sequence:return '原账单序号 '+sequence
                    return '原文件行 '+str(row['row']) if row.get('row') is not None else '来源记录 '+row['book_record_id'][-8:]
                items=[{**annotated(p),'records':[{**{k:rows[rid].get(k) for k in ('book_record_id','source','amount','currency','direction','counterparty','description','transaction_at','booking_at','trade_type','raw_status','payment_method','transaction_at_precision','booking_at_precision')},'source_label':source_label(rows[rid]),'effective_direction':flow_direction(rows[rid]),'refund':is_refund(rows[rid])} for rid in p['source_ids']]} for p in items]
            return {'items':items[(page-1)*size:page*size],'total':len(items),'page':page,'fingerprint':view['fingerprint'],'revision':view['revision']}
        if section=='privacy':
            _,candidates,masked=self.pairing_ai.preview(store,data,view)
            return {'candidates':masked,'count':len(candidates),'model':MODEL,'fingerprint':view['fingerprint'],'revision':view['revision']}
        return {k:v for k,v in view.items() if k not in {'transactions','relations','candidates','history'}} | {
            'relation_count':len(view['relations']),'pending_pairs':sum(p['decision']=='suggested' for p in view['candidates']),
            'ai_configured':bool(self.pairing_ai.key),'ai_model':MODEL,
            'restart_required':rules_need_restart(),
            'jobs':[{k:v for k,v in j.items() if k!='results'} for j in jobs]}

    def analysis_action(self,payload,ai=False):
        with self.lock:
            self.catalog.check(payload)
            if self.job['status']=='running':raise Conflict('导入期间暂停配对修改，请等待完成。')
            data=self.analysis_data();store=self.analysis_store(data['book_id'])
            if ai:
                if payload.get('action')=='cancel':return self.pairing_ai.cancel(store,payload.get('job_id'))
                if payload.get('action')!='start':raise ReviewError('不支持的 AI 操作。')
                return self.pairing_ai.start(store,data,store.view(data),payload)
            view=store.apply(data,payload)
            return {'revision':view['revision'],'fingerprint':view['fingerprint']}

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
