"""Append-only review events over an immutable, fingerprinted import bundle."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from jiaowopay_ingest.model import Row, digest, dumps
from jiaowopay_ingest.mapping import Mapping
from jiaowopay_ingest.normalize import normalize, parse_date, parse_money
from jiaowopay_ingest.storage import load_records, load_review_items, resolve_evidence
from .effective_policy import apply_reading_policy, VERSION as READING_POLICY
from .attention import field_attention


class ReviewError(ValueError):
    pass


class Conflict(ReviewError):
    pass


FIELDS = ('transaction_at', 'booking_at', 'amount', 'direction', 'currency',
          'balance', 'counterparty', 'description', 'account', 'source_record_id',
          'transaction_status')
EMPTY_STATE = {'overrides': {}, 'manual': {}, 'excluded': {}, 'pages': {}, 'undo_stack': []}
SUPPORTED_CURRENCIES = {'CNY', 'USD', 'EUR', 'HKD', 'JPY', 'GBP', 'CHF', 'AUD', 'CAD', 'SGD', 'NZD', 'KRW', 'TWD', 'MOP'}


def checked_fields(original, changes):
    if not isinstance(changes, dict) or set(changes) - set(FIELDS):
        raise ReviewError('包含不允许修改的字段。')
    row = copy.deepcopy(original)
    for key, value in changes.items():
        if value is not None and (not isinstance(value, str) or len(value) > 2000):
            raise ReviewError('字段必须是长度不超过 2000 的文本或空值。')
        row[key] = value.strip() or None if isinstance(value, str) else value
        if row[key] != original.get(key):
            row.get('derived_fields', {}).pop(key, None)
    for field in ('transaction_at', 'booking_at'):
        value, precision = parse_date(row.get(field))
        if row.get(field) and value is None:
            raise ReviewError('日期无法解释，请填写完整年月日及已知时间，不要猜测缺失时间。')
        if row.get(field) == original.get(field) and value is not None:
            precision = original.get(field + '_precision') or precision
        row[field], row[field + '_precision'] = value, precision
    if not row.get('transaction_at') and not row.get('booking_at'):
        raise ReviewError('至少填写交易日期或记账日期。')
    amount = parse_money(row.get('amount'))
    if amount is None or amount < 0:
        raise ReviewError('金额必须是明确的非负数；方向请单独选择。')
    row['amount'] = format(amount, 'f')
    if row.get('direction') not in {'in', 'out', 'none'}:
        raise ReviewError('请选择收入、支出或不计收支。')
    currency = row.get('currency') or ''
    if currency not in SUPPORTED_CURRENCIES:
        raise ReviewError('请填写原账单明确的受支持币种代码，例如 CNY；不可默认猜测。暂未支持的币种需先扩展并测试映射。')
    if row.get('balance') is not None:
        balance = parse_money(row['balance'])
        if balance is None:
            raise ReviewError('余额必须是明确数值或留空。')
        row['balance'] = format(balance, 'f')
    if row.get('transaction_status') not in {None, 'unknown', 'posted', 'pending', 'void'}:
        raise ReviewError('交易状态不支持。')
    row['transaction_status'] = row.get('transaction_status') or 'unknown'
    # A corrected amount/direction must not leave a contradictory derived amount.
    row['signed_amount'] = format(-amount if row['direction'] == 'out' else amount, 'f') if row['direction'] != 'none' else None
    row['human_confirmed'] = True
    return row


def change_summary(before, after, originals):
    """Small, human-readable before/after audit view (including compensating undo)."""
    changes = []
    ids = set()
    for name in ('overrides', 'manual', 'excluded'):
        ids.update(before[name]); ids.update(after[name])
    for rid in sorted(ids):
        left = before['overrides'].get(rid, before['manual'].get(rid, originals.get(rid)))
        right = after['overrides'].get(rid, after['manual'].get(rid, originals.get(rid)))
        for field in (*FIELDS, 'human_confirmed'):
            old, new = (left or {}).get(field), (right or {}).get(field)
            if old != new:
                changes.append({'record_id': rid, 'field': field, 'before': old, 'after': new})
        for field, old, new in [('record_exists', left is not None, right is not None),
                                ('excluded', rid in before['excluded'], rid in after['excluded'])]:
            if old != new:
                changes.append({'record_id': rid, 'field': field, 'before': old, 'after': new})
    for tid in sorted(set(before['pages']) | set(after['pages'])):
        if before['pages'].get(tid) != after['pages'].get(tid):
            changes.append({'review_id': tid, 'field': 'page_confirmation', 'before': before['pages'].get(tid), 'after': after['pages'].get(tid)})
    return changes


class ReviewStore:
    def __init__(self, bundle, state_root):
        self.bundle = Path(bundle).resolve()
        self.source_db = self.bundle / 'ledger.sqlite3'
        # Validate supported version and complete marker without modifying the source.
        self.originals = {r['record_id']: r for r in load_records(self.bundle, ready_only=False, allow_partial=True)}
        self.fingerprint = hashlib.sha256(self.source_db.read_bytes()).hexdigest()
        self.workspace_id = self.fingerprint
        self.state_dir = Path(state_root).resolve() / self.fingerprint
        if self.state_dir.is_relative_to(self.bundle):
            raise ReviewError('修订目录不能位于原始数据包内。')
        with closing(sqlite3.connect(self.source_db.as_uri() + '?mode=ro', uri=True)) as con:
            self.manifest = {k: json.loads(v) for k, v in con.execute('SELECT key,value FROM manifest')}
            self.files = [json.loads(r[0]) for r in con.execute('SELECT payload FROM files')]
            self.issues = [json.loads(r[0]) for r in con.execute('SELECT payload FROM issues')]
            self.raw = {r['raw_id']: r for (p,) in con.execute('SELECT payload FROM raw_rows') for r in [json.loads(p)]}
            self.assets = [r[0] for r in con.execute('SELECT path FROM assets')] if self.manifest.get('schema_version') == '1.1' else []
        self.tasks = {r['review_id']: r for r in load_review_items(self.bundle)}
        self.processing_records, self.issues, self.tasks = apply_reading_policy(
            self.originals, self.raw, self.issues, self.tasks, self.bundle)
        for file in self.files:
            affected = [r for r in self.processing_records.values() if r['file_id'] == file.get('file_id') and r.get('policy_adjustment')]
            if affected:
                file['original_status'] = file['status']
                file['status'] = 'partial' if any(i.get('file_id') == file.get('file_id') and i['severity'] == 'error' for i in self.issues) else 'completed'
        covered = {r.get('record_id') for r in self.tasks.values()}
        for record_id, row in self.processing_records.items():
            if row['parse_status'] == 'needs_review' and record_id not in covered:
                task_id = 'record-' + record_id
                reasons = [i for i in self.issues if i.get('record_id') == record_id]
                self.tasks[task_id] = {
                    'review_id': task_id, 'scope': 'record', 'record_id': record_id,
                    'file_id': row['file_id'], 'page': row.get('page'), 'evidence': {},
                    'reason_messages': [i['message'] for i in reasons if i['severity']=='error'],
                    'reason_codes': [i['code'] for i in reasons if i['severity']=='error'],
                }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = self.state_dir / 'review.sqlite3'
        with closing(self.connect()) as con, con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    revision INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL, command TEXT NOT NULL,
                    before_state TEXT NOT NULL, after_state TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            ''')
            con.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('fingerprint', self.fingerprint))
            if con.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()[0] != self.fingerprint:
                raise ReviewError('修订库与底稿不匹配。')

    def connect(self):
        return sqlite3.connect(self.db, timeout=10)

    def verify_source(self):
        if not (self.bundle / 'COMPLETE').is_file() or hashlib.sha256(self.source_db.read_bytes()).hexdigest() != self.fingerprint:
            raise ReviewError('原始数据包已变化，拒绝继续使用旧修订。')
        for relative in self.assets:
            resolve_evidence(self.bundle, relative)

    def _state(self, con):
        row = con.execute('SELECT revision,after_state FROM events ORDER BY revision DESC LIMIT 1').fetchone()
        return (row[0], json.loads(row[1])) if row else (0, copy.deepcopy(EMPTY_STATE))

    def _rows(self, state):
        rows = {**copy.deepcopy(self.processing_records), **copy.deepcopy(state['manual'])}
        for rid, override in state['overrides'].items():
            base = rows.get(rid, {})
            merged = {**base, **copy.deepcopy(override)}
            # Source coverage is regenerated; canonical human values (including blanks) win.
            for key in ('source_fields', 'unmapped_headers', 'semantic_policy', 'semantic_hints'):
                if key in base:
                    merged[key] = copy.deepcopy(base[key])
            merged['derived_fields'] = copy.deepcopy(override.get('derived_fields', {}))
            rows[rid] = merged
        for rid, row in rows.items():
            page_pending = any(t['scope'] == 'page' and t['file_id'] == row['file_id']
                               and t.get('page') == row.get('page') and t['review_id'] not in state['pages']
                               for t in self.tasks.values())
            excluded = rid in state['excluded']
            accepted = row.get('human_confirmed', False) or row.get('parse_status') == 'ready'
            row.update(review_status='excluded' if excluded else 'confirmed' if row.get('human_confirmed') else 'pending' if not accepted else 'parsed',
                       eligible_for_processing=bool(accepted and not excluded and not page_pending),
                       page_review_pending=page_pending,
                       original_parse_status=self.originals.get(rid, {}).get('parse_status'))
        return rows

    def _blockers(self, state, rows):
        blockers = []
        for item in self.issues:
            if item.get('severity') != 'error':
                continue
            rid = item.get('record_id')
            if rid and (rid in state['overrides'] or rid in state['excluded']):
                continue
            if item.get('page') is not None:
                matched = [t for t in self.tasks.values() if t['scope'] == 'page' and t['file_id'] == item.get('file_id') and t.get('page') == item['page']]
                if matched and all(t['review_id'] in state['pages'] for t in matched):
                    continue
            # An empty scanned file can be resolved explicitly through all its page tasks.
            if item['code'] == 'NO_TRANSACTIONS':
                pages = [t for t in self.tasks.values() if t['scope'] == 'page' and t['file_id'] == item.get('file_id')]
                if pages and all(t['review_id'] in state['pages'] for t in pages):
                    continue
            blockers.append(item)
        return blockers

    def snapshot(self):
        self.verify_source()
        with closing(self.connect()) as con:
            revision, state = self._state(con)
            history = [{'revision': r, 'created_at': at, **json.loads(cmd),
                        'changes': change_summary(json.loads(left), json.loads(right), self.originals)}
                       for r, at, cmd, left, right in con.execute('SELECT revision,created_at,command,before_state,after_state FROM events ORDER BY revision DESC')]
        rows = self._rows(state)
        tasks = []
        for t in self.tasks.values():
            item = copy.deepcopy(t)
            item['status'] = ('resolved' if item['review_id'] in state['pages'] else 'pending') if item['scope'] == 'page' else ('resolved' if rows[item['record_id']]['review_status'] in {'confirmed', 'excluded'} else 'pending')
            if item['scope'] == 'record' and rows[item['record_id']]['review_status'] == 'parsed':
                item['status'] = 'resolved_by_policy'
            tasks.append(item)
        blockers = self._blockers(state, rows)
        return {'workspace_id': self.workspace_id, 'revision': revision, 'reading_policy': READING_POLICY,
                'batch_id': self.manifest['batch_id'], 'source_status': self.manifest['status'],
                'records': list(rows.values()), 'tasks': tasks, 'files': self.files,
                'issues': self.issues, 'blockers': blockers, 'history': history,
                'undo_revision': state['undo_stack'][-1] if state['undo_stack'] else None,
                'summary': {'total': len(rows), 'eligible': sum(r['eligible_for_processing'] for r in rows.values()),
                            'pending': sum(r['review_status'] == 'pending' for r in rows.values()),
                            'excluded': len(state['excluded']), 'manual': len(state['manual']),
                            'page_pending': sum(t['scope'] == 'page' and t['status'] == 'pending' for t in tasks),
                            'blocking_issues': len(blockers)}}

    def detail(self, record_id):
        snap = self.snapshot()
        row = next((r for r in snap['records'] if r['record_id'] == record_id), None)
        if row is None:
            raise ReviewError('记录不存在。')
        evidence = None
        path = row.get('ocr_evidence', {}).get('ocr_json')
        if row['review_status'] == 'pending' and path:
            evidence = json.loads(self.evidence(path).read_text(encoding='utf8'))
        return {'record': row, 'original': self.originals.get(record_id),
                'raw': self.raw.get(row.get('raw_id')), 'revision': snap['revision'],
                'attention': field_attention(row, self.originals.get(record_id, row), snap['issues'], evidence)}

    def _invalidate_page(self, state, row):
        for t in self.tasks.values():
            if t['scope'] == 'page' and t['file_id'] == row['file_id'] and t.get('page') == row.get('page'):
                state['pages'].pop(t['review_id'], None)

    def apply(self, command):
        self.verify_source()
        if not isinstance(command, dict):
            raise ReviewError('请求必须是对象。')
        if command.get('workspace_id') != self.workspace_id:
            raise Conflict('当前账本已切换，请刷新后重试。')
        request_id = command.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch('[A-Za-z0-9-]{8,80}', request_id):
            raise ReviewError('缺少有效请求标识。')
        # Confirmation is itself an audited action; a user-written explanation is optional.
        # Keep the original request intact for replay comparison and never invent a reason.
        note = command.get('note', '')
        if (not isinstance(note, str) or len(note) > 2000
                or (command.get('action') != 'confirm' and not note.strip())):
            raise ReviewError('请填写核验依据或修改原因（最多 2000 字）。')
        with closing(self.connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            existing = con.execute('SELECT revision,command FROM events WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if json.loads(existing[1]) != command:
                    raise Conflict('同一请求标识不能用于不同操作。')
                return {'revision': existing[0], 'replayed': True}
            revision, before = self._state(con)
            if type(command.get('expected_revision')) is not int or command['expected_revision'] != revision:
                raise Conflict('数据已被其他操作更新，请刷新并重新核对。')
            state = copy.deepcopy(before)
            rows = self._rows(state)
            action = command.get('action')
            if action == 'undo':
                if not state['undo_stack'] or command.get('target_revision') != state['undo_stack'][-1]:
                    raise Conflict('只能撤销当前最近一次尚未撤销的操作。')
                state = json.loads(con.execute('SELECT before_state FROM events WHERE revision=?', (command['target_revision'],)).fetchone()[0])
            elif action in {'confirm', 'exclude'}:
                rid = command.get('record_id')
                if rid not in rows:
                    raise ReviewError('记录不存在。')
                row = rows[rid]
                self._invalidate_page(state, row)
                if action == 'confirm':
                    state['overrides'][rid] = checked_fields(row, command.get('fields', {}))
                    state['excluded'].pop(rid, None)
                else:
                    state['excluded'][rid] = note
            elif action == 'add':
                fid = command.get('file_id')
                file = next((f for f in self.files if f.get('file_id') == fid and f['status'] != 'duplicate_file_skipped'), None)
                if not file:
                    raise ReviewError('请选择有效来源文件。')
                page = command.get('page')
                if page is not None and (type(page) is not int or not 1 <= page <= 300):
                    raise ReviewError('页码必须为 1–300 的整数或空值。')
                if file.get('format') in {'pdf', 'image', 'png', 'jpeg'} and page is None:
                    raise ReviewError('图片/PDF 补录必须填写原页码。')
                maximum = 1 if file.get('format') in {'image', 'png', 'jpeg'} else file.get('reader_metadata', {}).get('page_count')
                if maximum and page is not None and page > maximum:
                    raise ReviewError('补录页码超出来源文件页数。')
                rid = 'manual-' + digest(self.workspace_id, request_id)
                template, _ = normalize(Row([], 0), Mapping({}, [], 0))
                row = checked_fields({**template, 'record_id': rid, 'raw_id': None, 'file_id': fid,
                                      'filename': file['filename'], 'page': page,
                                      'parse_status': 'needs_review', 'extraction_method': 'manual',
                                      'profile': 'manual.v1', 'mapping_method': 'human_confirmed',
                                      'segment': 'manual', 'kind': 'manual', 'sheet': None,
                                      'table': None, 'row': None, 'end_row': None,
                                      'mapping_columns': {}, 'raw_headers': [],
                                      'source_note': note}, command.get('fields', {}))
                state['manual'][rid] = row
                self._invalidate_page(state, row)
            elif action == 'resolve_page':
                tid = command.get('review_id')
                task = self.tasks.get(tid)
                if not task or task['scope'] != 'page':
                    raise ReviewError('整页任务不存在。')
                page_rows = [r for r in rows.values() if r['file_id'] == task['file_id'] and r.get('page') == task.get('page') and r['review_status'] != 'excluded']
                if any(r['review_status'] == 'pending' for r in page_rows):
                    raise ReviewError('请先确认或排除本页待定记录，再核对总笔数。')
                count = command.get('observed_count')
                if type(count) is not int or count < 0 or count != len(page_rows):
                    raise ReviewError('人工核对的交易笔数与当前本页记录数不一致，请先补录或排除误识别行。')
                state['pages'][tid] = {'count': count, 'note': note}
            else:
                raise ReviewError('不支持的核验操作。')
            if action != 'undo':
                state['undo_stack'].append(revision + 1)
            con.execute('INSERT INTO events VALUES (?,?,?,?,?,?)',
                        (revision + 1, request_id, datetime.now(timezone.utc).isoformat(),
                         dumps(command), dumps(before), dumps(state)))
        return {'revision': revision + 1, 'replayed': False}

    def evidence(self, relative):
        self.verify_source()
        return resolve_evidence(self.bundle, relative)


def load_effective_records(bundle, state_root, *, allow_partial=False):
    """Versioned output for later reconciliation; never silently omit pending rows."""
    store = ReviewStore(bundle, state_root)
    snap = store.snapshot()
    incomplete = bool(snap['blockers'] or snap['summary']['pending'] or snap['summary']['page_pending'])
    if incomplete and not allow_partial:
        raise ReviewError('还有未解决的记录、页面或导入错误；拒绝输出完整账本。可显式接受部分数据。')
    return {'workspace_id': snap['workspace_id'], 'revision': snap['revision'], 'reading_policy': snap['reading_policy'],
            'source_files':[{'file_id':f['file_id'],'sha256':f.get('sha256'),'filename':f['filename']} for f in snap['files'] if f.get('file_id')],
            'partial': incomplete, 'summary': snap['summary'],
            'records': [r for r in snap['records'] if r['eligible_for_processing']],
            'excluded_record_ids': [r['record_id'] for r in snap['records'] if r['review_status'] == 'excluded'],
            'pending_record_ids': [r['record_id'] for r in snap['records'] if not r['eligible_for_processing'] and r['review_status'] != 'excluded'],
            'blockers': snap['blockers'], 'transaction_deduplication': False}
