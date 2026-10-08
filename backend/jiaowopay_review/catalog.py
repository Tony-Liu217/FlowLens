"""Local named books; immutable bundles and review databases remain separate."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .store import ReviewError, Conflict, ReviewStore


def now():
    return datetime.now(timezone.utc).isoformat()


def name_value(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80 or re.search(r'[\x00-\x1f]', value):
        raise ReviewError('账本名称请填写 1–80 个字符，不含控制字符。')
    return value.strip()


class Catalog:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / 'books.sqlite3'
        with closing(self.connect()) as con, con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS books (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL,
                  updated TEXT NOT NULL, deleted TEXT, purged INTEGER NOT NULL DEFAULT 0,
                  bundle TEXT, states TEXT NOT NULL, fingerprint TEXT,
                  source_key TEXT UNIQUE, owned INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS batches (
                  id TEXT PRIMARY KEY, book_id TEXT NOT NULL, name TEXT NOT NULL,
                  created TEXT NOT NULL, bundle TEXT NOT NULL, states TEXT NOT NULL,
                  fingerprint TEXT NOT NULL, UNIQUE(book_id,bundle));
                INSERT OR IGNORE INTO settings VALUES ('revision','0');
                INSERT OR IGNORE INTO settings VALUES ('active','');
            ''')
        self.warnings = []
        self.relocate()

    def relocate(self):
        """Rebase managed paths when the whole data directory moves.

        Immutable bundles and review events are not rewritten. External bundles
        intentionally keep their absolute paths; they must be copied separately.
        """
        with closing(self.connect()) as con, con:
            row=con.execute("SELECT value FROM settings WHERE key='data_root'").fetchone()
            previous=Path(row[0]) if row else self.root
            def rebase(value):
                if not value:return value
                path=Path(value)
                return str(self.root/path.relative_to(previous)) if path.is_relative_to(previous) else value
            if previous!=self.root:
                for book in con.execute('SELECT id,bundle,states,source_key FROM books').fetchall():
                    con.execute('UPDATE books SET bundle=?,states=?,source_key=? WHERE id=?',
                                (*[rebase(book[k]) for k in ('bundle','states','source_key')],book['id']))
                for batch in con.execute('SELECT id,bundle,states FROM batches').fetchall():
                    con.execute('UPDATE batches SET bundle=?,states=? WHERE id=?',
                                (rebase(batch['bundle']),rebase(batch['states']),batch['id']))
                pointer=self.root/'last-bundle.json'
                if pointer.is_file():
                    try:
                        data=json.loads(pointer.read_text(encoding='utf-8'))
                        data['bundle']=rebase(data['bundle'])
                    except (ValueError,KeyError,TypeError):
                        pass  # Existing migration reports malformed pointers.
                    else:
                        temp=pointer.with_suffix('.relocating')
                        temp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
                        temp.replace(pointer)
                self.bump(con)
            con.execute("INSERT OR REPLACE INTO settings VALUES ('data_root',?)",(str(self.root),))

    def connect(self):
        con = sqlite3.connect(self.db, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def context(self):
        with closing(self.connect()) as con:
            values = dict(con.execute('SELECT key,value FROM settings'))
        return {'book_id': values['active'] or None, 'catalog_revision': int(values['revision'])}

    def check(self, payload):
        if not isinstance(payload, dict):
            raise ReviewError('请求格式无效。')
        ctx = self.context()
        if payload.get('book_id') != ctx['book_id'] or payload.get('catalog_revision') != ctx['catalog_revision']:
            raise Conflict('账本或管理版本已变化，请刷新后重试；旧页面的操作未保存。')

    def bump(self, con, active=None, change_active=False):
        con.execute("UPDATE settings SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")
        if change_active:
            con.execute("UPDATE settings SET value=? WHERE key='active'", (active or '',))

    def get(self, book_id):
        with closing(self.connect()) as con:
            row = con.execute('SELECT * FROM books WHERE id=? AND purged=0', (book_id,)).fetchone()
        if not row:
            raise ReviewError('账本不存在或已彻底删除。')
        return dict(row)

    def rows(self):
        with closing(self.connect()) as con:
            return [dict(r) for r in con.execute('SELECT * FROM books WHERE purged=0 ORDER BY updated DESC,id')]

    def create(self, name):
        name = name_value(name)
        bid = uuid.uuid4().hex
        stamp = now()
        states = self.root / 'books' / bid / 'revisions'
        with closing(self.connect()) as con, con:
            con.execute('INSERT INTO books(id,name,created,updated,states) VALUES (?,?,?,?,?)', (bid,name,stamp,stamp,str(states)))
            self.bump(con,bid,True)
        return bid

    def select(self, bid):
        book = self.get(bid)
        if book['deleted']:
            raise ReviewError('请先从回收站恢复账本。')
        with closing(self.connect()) as con, con:
            self.bump(con,bid,True)

    def batches(self, bid):
        self.get(bid)
        with closing(self.connect()) as con:
            return [dict(r) for r in con.execute('SELECT * FROM batches WHERE book_id=? ORDER BY created,id',(bid,))]

    def selected_batch(self, bid):
        batches=self.batches(bid)
        with closing(self.connect()) as con:
            row=con.execute('SELECT value FROM settings WHERE key=?',('batch:'+bid,)).fetchone()
        return next((b for b in batches if row and b['id']==row[0]),batches[-1] if batches else None)

    def select_batch(self, bid, batch_id):
        if not any(b['id']==batch_id for b in self.batches(bid)):
            raise ReviewError('批次不属于当前账本。')
        with closing(self.connect()) as con, con:
            con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('batch:'+bid,batch_id))
            self.bump(con)

    def migrate_batches(self):
        # Metadata-only migration: keep original bundles and append-only reviews in place.
        with closing(self.connect()) as con, con:
            con.execute('''INSERT OR IGNORE INTO batches(id,book_id,name,created,bundle,states,fingerprint)
              SELECT id,id,'第 1 批',created,bundle,states,fingerprint FROM books
              WHERE purged=0 AND bundle IS NOT NULL''')

    def attach(self, bid, bundle, name=None):
        book = self.get(bid)
        if book['deleted']:
            raise Conflict('该账本已删除。')
        batch_id=uuid.uuid4().hex
        name=name_value(name) if name else '第 %s 批' % (len(self.batches(bid))+1)
        states=Path(book['states']) if not book['bundle'] else self.root/'books'/bid/'batch-revisions'/batch_id
        store = ReviewStore(bundle,states)
        store.snapshot()
        with closing(self.connect()) as con, con:
            stamp=now()
            con.execute('INSERT INTO batches VALUES (?,?,?,?,?,?,?)',
                        (batch_id,bid,name,stamp,str(store.bundle),str(states),store.fingerprint))
            # Keep the first-batch pointer for legacy migration and safe legacy purge.
            con.execute('UPDATE books SET bundle=COALESCE(bundle,?),fingerprint=COALESCE(fingerprint,?),updated=? WHERE id=?', (str(store.bundle),store.fingerprint,stamp,bid))
            con.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('batch:'+bid,batch_id))
            self.bump(con)
        return store

    def touch(self, bid):
        with closing(self.connect()) as con, con:
            con.execute('UPDATE books SET updated=? WHERE id=?', (now(),bid))

    def change(self, action, bid, name=None):
        book = self.get(bid)
        if action == 'rename':
            if book['deleted']: raise ReviewError('请先恢复账本，再重命名。')
            name = name_value(name)
        elif action == 'restore':
            if not book['deleted']: raise ReviewError('该账本不在回收站。')
        elif action in {'trash','reset'}:
            if book['deleted']: raise ReviewError('该账本已在回收站。')
        else:
            raise ReviewError('不支持的账本操作。')
        ctx = self.context()
        with closing(self.connect()) as con, con:
            if action == 'rename':
                con.execute('UPDATE books SET name=?,updated=? WHERE id=?',(name,now(),bid))
                self.bump(con)
            elif action == 'restore':
                con.execute('UPDATE books SET deleted=NULL,updated=? WHERE id=?',(now(),bid))
                self.bump(con)
            else:
                con.execute('UPDATE books SET deleted=?,updated=? WHERE id=?',(now(),now(),bid))
                if action == 'reset':
                    new_id=uuid.uuid4().hex
                    states=self.root/'books'/new_id/'revisions'
                    con.execute('INSERT INTO books(id,name,created,updated,states) VALUES (?,?,?,?,?)',(new_id,book['name'],now(),now(),str(states)))
                    self.bump(con,new_id,True)
                else:
                    self.bump(con,None,ctx['book_id']==bid)

    def register(self, bundle):
        bundle=Path(bundle).resolve()
        key=str(bundle)
        with closing(self.connect()) as con:
            existing=con.execute('SELECT id,purged FROM books WHERE source_key=?',(key,)).fetchone()
        if existing:
            return None if existing['purged'] else existing['id']
        # Read/validate source using the existing revision store, preserving its history.
        legacy=self.root/'revisions'
        store=ReviewStore(bundle,legacy)
        stamp=store.manifest.get('created_at') or now()
        filenames='、'.join(f['filename'] for f in store.files)
        name=(stamp[:10]+' '+(filenames or '历史账本'))[:80]
        bid=uuid.uuid4().hex
        states=legacy
        with closing(self.connect()) as con:
            shared=con.execute('SELECT 1 FROM books WHERE fingerprint=? AND states=? AND purged=0',(store.fingerprint,str(legacy))).fetchone()
        if shared:
            states=self.root/'books'/bid/'revisions'
            destination=states/store.fingerprint
            destination.mkdir(parents=True)
            with closing(sqlite3.connect(store.db)) as src, closing(sqlite3.connect(destination/'review.sqlite3')) as dst:
                src.backup(dst)
        owned=bundle.parent == (self.root/'imports').resolve()
        with closing(self.connect()) as con, con:
            con.execute('INSERT INTO books(id,name,created,updated,bundle,states,fingerprint,source_key,owned) VALUES (?,?,?,?,?,?,?,?,?)',
                        (bid,name,stamp,now(),str(bundle),str(states),store.fingerprint,key,int(owned)))
            self.bump(con)
        return bid

    def migrate(self, explicit=None):
        # Scan only immediate legacy imports; COMPLETE includes failed but fully saved batches.
        imports=self.root/'imports'
        candidates=sorted(p for p in imports.iterdir() if p.is_dir() and not p.is_symlink()) if imports.exists() else []
        pointer=self.root/'last-bundle.json'
        previous=None
        if pointer.is_file():
            try: previous=Path(json.loads(pointer.read_text(encoding='utf-8'))['bundle']).resolve()
            except (ValueError,KeyError,TypeError): self.warnings.append('旧的最近账本记录无法读取，已保留原文件。')
        if previous: candidates.append(previous)
        if explicit: candidates.append(Path(explicit).resolve())
        selected=None
        for path in dict.fromkeys(candidates):
            if not (path/'COMPLETE').is_file():
                self.warnings.append('发现未完整保存的历史批次，未作为可用账本登记。')
                continue
            try:
                bid=self.register(path)
                if path==(Path(explicit).resolve() if explicit else previous): selected=bid
            except (ValueError,OSError,sqlite3.Error):
                self.warnings.append('有历史批次无法登记，原数据保留，请检查数据包与证据。')
        with closing(self.connect()) as con, con:
            migrated=con.execute("SELECT value FROM settings WHERE key='migrated'").fetchone()
            if (not migrated or explicit) and selected and not self.get(selected)['deleted']:
                self.bump(con,selected,True)
            con.execute("INSERT OR REPLACE INTO settings VALUES ('migrated','1')")
        self.migrate_batches()

    def purge(self, bid, confirmation):
        book=self.get(bid)
        if not book['deleted'] or confirmation!=book['name']:
            raise ReviewError('彻底删除仅适用于回收站中的账本，请输入完整账本名称确认。')
        targets=[]
        own=self.root/'books'/bid
        if not re.fullmatch('[0-9a-f]{32}',bid): raise ReviewError('账本标识无效。')
        for batch in self.batches(bid):
            if batch['bundle']==book['bundle']:
                if batch['states']!=book['states'] or batch['fingerprint']!=book['fingerprint']:
                    raise ReviewError('历史批次关联异常，拒绝删除。')
            elif not Path(batch['bundle']).is_relative_to(own) or not Path(batch['states']).is_relative_to(own):
                raise ReviewError('追加批次路径超出该账本目录，拒绝删除。')
        if own.exists(): targets.append(own)
        if book['owned'] and book['bundle']:
            bundle=Path(book['bundle'])
            if bundle.parent==self.root/'imports': targets.append(bundle)
            elif not bundle.is_relative_to(own): raise ReviewError('数据包路径超出该账本目录。')
        if book['fingerprint'] and Path(book['states'])==self.root/'revisions':
            if not re.fullmatch('[0-9a-f]{64}',book['fingerprint']): raise ReviewError('底稿标识无效。')
            targets.append(self.root/'revisions'/book['fingerprint'])
        others=[b for b in self.rows() if b['id']!=bid]
        # Validate ALL absolute targets before any deletion; reject symlinks/junctions.
        for target in targets:
            if target.resolve()!=target.absolute() or not target.resolve().is_relative_to(self.root) or target.resolve()==self.root:
                raise ReviewError('删除路径不安全，未删除账本。')
            for path in [target,*target.rglob('*')] if target.exists() else []:
                if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
                    raise ReviewError('账本中存在链接目录，拒绝递归删除。')
            for other in others:
                refs=[Path(other['bundle'])] if other['bundle'] else []
                if other['fingerprint']: refs.append(Path(other['states'])/other['fingerprint'])
                for batch in self.batches(other['id']):
                    refs.extend([Path(batch['bundle']),Path(batch['states'])/batch['fingerprint']])
                if any(p.is_relative_to(target) for p in refs): raise ReviewError('目录仍被其他账本引用，拒绝删除。')
        for target in targets:
            if target.exists(): shutil.rmtree(target)
        with closing(self.connect()) as con, con:
            # Keep a path tombstone so a legacy pointer cannot resurrect a deleted book.
            con.execute('UPDATE books SET purged=1,bundle=NULL,name=?,fingerprint=NULL WHERE id=?',('已彻底删除',bid))
            con.execute('DELETE FROM batches WHERE book_id=?',(bid,))
            con.execute('DELETE FROM settings WHERE key=?',('batch:'+bid,))
            self.bump(con)
