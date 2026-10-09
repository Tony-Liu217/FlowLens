"""Independent append-only decisions and versioned derived snapshots per book."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from jiaowopay_ingest.model import digest
from jiaowopay_review.store import ReviewError, Conflict
from .core import build, fingerprint


class AnalysisStore:
    def __init__(self, directory):
        self.directory=Path(directory); self.directory.mkdir(parents=True,exist_ok=True)
        self.db=self.directory/'analysis.sqlite3'; self.cache=None
        with closing(self.connect()) as con, con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS decisions (
                  revision INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
                  request_hash TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshots (
                  fingerprint TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                  PRIMARY KEY(fingerprint,revision));
                CREATE TABLE IF NOT EXISTS ai_jobs (
                  id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')

    def connect(self):
        return sqlite3.connect(self.db,timeout=15)

    def events(self):
        with closing(self.connect()) as con:
            return [{'revision':r[0],**json.loads(r[1]),'created':r[2]} for r in con.execute('SELECT revision,payload,created FROM decisions ORDER BY revision')]

    def state(self):
        events=self.events();active={};undone=set()
        for e in events:
            if e['action']=='undo':undone.add(e['target'])
        for e in events:
            if e['action']!='undo' and e['revision'] not in undone:active[e['relation_id']]=e
        # Undoing acceptance means keep this relation split until explicitly accepted again.
        for e in events:
            if e['action']=='undo':
                original=next(x for x in events if x['revision']==e['target'])
                pid=original['relation_id']
                if original['action']=='accept' and (pid not in active or active[pid]['revision']<e['revision']):
                    active[pid]={**original,'action':'reject','revision':e['revision']}
        return events,active

    def view(self,data):
        fp=fingerprint(data);events,active=self.state();revision=events[-1]['revision'] if events else 0
        if self.cache and (self.cache['fingerprint'],self.cache['revision'])==(fp,revision):return self.cache
        with closing(self.connect()) as con:
            saved=con.execute('SELECT payload FROM snapshots WHERE fingerprint=? AND revision=?',(fp,revision)).fetchone()
        if saved:
            result=json.loads(saved[0])
        else:
            result=build(data,active);result['revision']=revision
            result['history']=events
            # User-selected display overrides live on the result, not on source rows.
            for event in active.values():
                if event['action']!='display':continue
                tx=next((t for t in result['transactions'] if t['id']==event['relation_id']),None)
                if tx and event['dependencies']=={r['book_record_id']:digest(r) for r in tx['sources']}:
                    tx['display_override']=event['fields'];tx.update(event['fields'])
                else:result['stale_decisions'].append(event['relation_id'])
            with closing(self.connect()) as con,con:
                con.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)',(fp,revision,json.dumps(result,ensure_ascii=False)))
        self.cache=result
        return result

    def apply(self,data,payload):
        request_id=payload.get('request_id')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ReviewError('缺少有效请求标识。')
        request_hash=digest(payload)
        with closing(self.connect()) as con:
            old=con.execute('SELECT request_hash FROM decisions WHERE request_id=?',(request_id,)).fetchone()
        if old:
            if old[0]!=request_hash:raise Conflict('请求标识已用于其他操作。')
            return self.view(data)
        view=self.view(data)
        if payload.get('fingerprint')!=view['fingerprint'] or payload.get('expected_revision')!=view['revision']:
            raise Conflict('来源或配对版本已变化，请刷新后重试。')
        action=payload.get('action');pid=payload.get('relation_id')
        if action not in {'accept','reject','defer','undo','display'}:raise ReviewError('不支持的配对操作。')
        event={'action':action,'relation_id':pid,'input':view['fingerprint']}
        if action=='undo':
            target=payload.get('target')
            original=next((e for e in view['history'] if e['revision']==target and e['action']!='undo'),None)
            if not original or any(e.get('target')==target for e in view['history']):raise Conflict('决定不存在或已撤销。')
            event.update(target=target,relation_id=original['relation_id'])
        elif action=='display':
            tx=next((t for t in view['transactions'] if t['id']==pid),None);fields=payload.get('fields')
            if not tx or not isinstance(fields,dict) or not fields or set(fields)-{'counterparty','description'}:
                raise ReviewError('仅支持修改交易显示对象和说明。')
            if any(not isinstance(v,str) or len(v)>2000 for v in fields.values()):raise ReviewError('显示文本过长或格式无效。')
            event.update(fields=fields,dependencies={r['book_record_id']:digest(r) for r in tx['sources']})
        else:
            proposal=next((p for p in view['relations']+view['candidates'] if p['id']==pid),None)
            if not proposal:raise Conflict('配对已失效，请刷新。')
            if action=='accept' and proposal.get('decision')=='conflict':raise Conflict('来源已被其他关系占用，请先拆开已有关系。')
            event.update(dependencies=proposal['dependencies'],proposal=proposal)
            # Verify user acceptance against the exact same allocation constraints.
            if action=='accept':
                _,active=self.state();test=build(data,{**active,pid:event})
                if not any(p['id']==pid for p in test['relations']):raise Conflict('配对与现有关系或金额额度冲突，未保存。')
        with closing(self.connect()) as con,con:
            con.execute('BEGIN IMMEDIATE')
            rev=con.execute('SELECT COALESCE(MAX(revision),0) FROM decisions').fetchone()[0]
            if rev!=view['revision']:raise Conflict('配对决定已变化，请刷新。')
            con.execute('INSERT INTO decisions(request_id,request_hash,payload,created) VALUES (?,?,?,?)',
                        (request_id,request_hash,json.dumps(event,ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        self.cache=None
        return self.view(data)
