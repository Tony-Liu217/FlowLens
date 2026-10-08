"""Immutable run bundles; SQLite is the canonical data store, XLSX is a view."""
from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from . import SCHEMA_VERSION
from .model import dumps


def save_bundle(dataset: dict, output_dir, *, excel: bool = True) -> Path:
    target = Path(output_dir)
    assets=dataset.get('assets',[])
    data=dataset.get('_assets',{})
    for item in assets:
        safe_relative(item['path'])
        content=data.get(item['path'])
        if content is None or hashlib.sha256(content).hexdigest()!=item['sha256'] or len(content)!=item['bytes']:
            raise ValueError('截图或识别证据缺失/不一致，拒绝保存完整数据包。')
    # Fail closed on existing directories: never overwrite a previous run or sources.
    target.mkdir(parents=True, exist_ok=False)
    db = target / "ledger.sqlite3"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript("""
            CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE files (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE segments (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE raw_rows (raw_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, disposition TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE records (record_id TEXT PRIMARY KEY, raw_id TEXT NOT NULL REFERENCES raw_rows(raw_id), file_id TEXT NOT NULL,
                parse_status TEXT NOT NULL CHECK(parse_status IN ('ready','needs_review')), amount TEXT, currency TEXT,
                direction TEXT, transaction_at TEXT, booking_at TEXT, payload TEXT NOT NULL);
            CREATE TABLE issues (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE reviews (review_id TEXT PRIMARY KEY, record_id TEXT REFERENCES records(record_id), status TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE assets (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX records_status ON records(parse_status);
        """)
        manifest = {k:v for k,v in dataset.items() if k not in {"files", "segments", "raw_rows", "records", "issues",'reviews','assets','_assets'}}
        con.executemany("INSERT INTO manifest VALUES (?,?)", [(k,dumps(v)) for k,v in manifest.items()])
        for name in ("files", "segments", "issues"):
            con.executemany(f"INSERT INTO {name}(payload) VALUES (?)", [(dumps(x),) for x in dataset[name]])
        con.executemany("INSERT INTO raw_rows VALUES (?,?,?,?)", [(r["raw_id"],r["file_id"],r["disposition"],dumps(r)) for r in dataset["raw_rows"]])
        con.executemany("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)", [(r["record_id"],r["raw_id"],r["file_id"],r["parse_status"],r["amount"],r["currency"],r["direction"],r["transaction_at"],r["booking_at"],dumps(r)) for r in dataset["records"]])
        con.executemany('INSERT INTO reviews VALUES (?,?,?,?)',[(r['review_id'],r.get('record_id'),r['status'],dumps(r)) for r in dataset.get('reviews',[])])
        con.executemany('INSERT INTO assets VALUES (?,?,?)',[(a['path'],a['sha256'],dumps(a)) for a in assets])
    for item in assets:
        destination=target/item['path']
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(data[item['path']])
    # Human/tool-readable interchange: schema-preserving JSON, not inferred CSV types.
    for name, value in (("manifest.json", {**manifest, "files": dataset["files"], "segments": dataset["segments"]}),):
        (target/name).write_text(dumps(value), encoding="utf-8")
    for name in ("records", "raw_rows", "issues",'reviews','assets'):
        with (target/(name+".jsonl")).open("w", encoding="utf-8", newline="\n") as stream:
            for row in dataset.get(name,[]):
                stream.write(dumps(row)+"\n")
    if excel:
        from .excel import export_xlsx
        export_xlsx(dataset, target/"标准化流水.xlsx")
    from .review import export_review_html
    export_review_html(dataset,target/'review.html')
    (target/"COMPLETE").write_text("Import bundle saved successfully.\n", encoding="utf-8")
    return target


def load_records(bundle, *, ready_only: bool = True, allow_partial: bool = False) -> list[dict]:
    """Explicitly opt in to partial datasets; amounts remain exact decimal strings.

    ready means parsed, NOT paid/settled/deduplicated. Consumers must inspect status.
    """
    bundle = Path(bundle)
    if not (bundle/"COMPLETE").is_file():
        raise ValueError("导入数据包未完整写入，拒绝读取。")
    with closing(sqlite3.connect((bundle/"ledger.sqlite3").resolve().as_uri()+"?mode=ro", uri=True)) as con:
        manifest = {k:json.loads(v) for k,v in con.execute("SELECT key,value FROM manifest")}
        if manifest.get("schema_version") not in {'1.0',SCHEMA_VERSION}:
            raise ValueError("不支持的数据版本。")
        if manifest.get("status") != "completed" and not allow_partial:
            raise ValueError("此批次有异常或失败文件；先核对，或显式指定 allow_partial=True。")
        where = " WHERE parse_status='ready'" if ready_only else ""
        return [json.loads(row[0]) for row in con.execute("SELECT payload FROM records"+where+" ORDER BY rowid")]


def safe_relative(relative):
    from pathlib import PurePosixPath
    if not isinstance(relative,str):raise ValueError('证据路径必须是字符串。')
    p=PurePosixPath(relative)
    if '\\' in relative or ':' in relative or p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0]!='evidence':
        raise ValueError('证据路径必须是数据包内的 evidence 相对路径。')
    return p


def load_review_items(bundle):
    bundle=Path(bundle)
    if not (bundle/'COMPLETE').is_file():raise ValueError('数据包未写完。')
    with closing(sqlite3.connect((bundle/'ledger.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as con:
        version=con.execute("SELECT value FROM manifest WHERE key='schema_version'").fetchone()
        if not version or json.loads(version[0]) not in {'1.0',SCHEMA_VERSION}:raise ValueError('不支持的数据版本。')
        if json.loads(version[0])=='1.0':return []
        return [json.loads(r[0]) for r in con.execute('SELECT payload FROM reviews ORDER BY rowid')]


def resolve_evidence(bundle, relative):
    """Resolve a registered asset with traversal/symlink and SHA-256 checks."""
    safe_relative(relative)
    bundle=Path(bundle).resolve()
    if not (bundle/'COMPLETE').is_file():raise ValueError('数据包未写完。')
    path=(bundle/relative).resolve()
    if not path.is_relative_to(bundle):raise ValueError('证据路径越界。')
    with closing(sqlite3.connect((bundle/'ledger.sqlite3').as_uri()+'?mode=ro',uri=True)) as con:
        row=con.execute('SELECT sha256 FROM assets WHERE path=?',(relative,)).fetchone()
    if not row or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row[0]:
        raise ValueError('证据不存在或已被修改。')
    return path
