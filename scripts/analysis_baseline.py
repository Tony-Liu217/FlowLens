"""Create a private isolated single-book fixture using SQLite backups, never write source."""
import collections
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def digest_database(path):
    with readonly(path) as con:
        return hashlib.sha256('\n'.join(con.iterdump()).encode()).hexdigest()


def main():
    source = ROOT / '.local-data/workspace'
    target = ROOT / '.local-data' / ('analysis-baseline-' + str(time.time_ns())) / 'workspace'
    with readonly(source / 'books.sqlite3') as con:
        con.row_factory = sqlite3.Row
        books = list(con.execute('SELECT * FROM books WHERE deleted IS NULL AND purged=0'))
        if len(sys.argv)>1:
            books=[b for b in books if b['id']==sys.argv[1]]
        if len(books) != 1:
            raise RuntimeError('Select an explicit baseline book before copying: active book count is not one.')
        book = dict(books[0])
        batches = [dict(b) for b in con.execute('SELECT * FROM batches WHERE book_id=?', (book['id'],))]
    directory = source / 'books' / book['id']
    if any(not Path(b['bundle']).resolve().is_relative_to(directory.resolve()) or
           not Path(b['states']).resolve().is_relative_to(directory.resolve()) for b in batches):
        raise RuntimeError('External bundles require an explicit consistent copy plan.')
    databases = [source / 'books.sqlite3', *directory.rglob('*.sqlite3')]
    before = {str(p.relative_to(source)): digest_database(p) for p in databases}
    target.mkdir(parents=True)
    shutil.copytree(directory, target / 'books' / book['id'], ignore=shutil.ignore_patterns('*.sqlite3', '*-wal', '*-shm'))
    for original in databases:
        copied = target / original.relative_to(source)
        copied.parent.mkdir(parents=True, exist_ok=True)
        with readonly(original) as src, sqlite3.connect(copied) as dst:
            src.backup(dst)
    after = {str(p.relative_to(source)): digest_database(p) for p in databases}
    copied_hashes = {str(p.relative_to(source)): digest_database(target / p.relative_to(source)) for p in databases}
    if before != after or after != copied_hashes:
        raise RuntimeError('Source changed during backup; reject this snapshot and rerun when idle.')
    with sqlite3.connect(target / 'books.sqlite3') as con:
        con.execute('DELETE FROM batches WHERE book_id<>?', (book['id'],))
        con.execute('DELETE FROM books WHERE id<>?', (book['id'],))
        con.execute("UPDATE settings SET value=? WHERE key='active'", (book['id'],))
    from jiaowopay_review.catalog import Catalog
    from jiaowopay_review.bookstore import export_book
    catalog = Catalog(target)
    data = export_book(catalog, book['id'], allow_partial=True)
    (target.parent / 'effective-private.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf8')
    sources = {}
    for row in data['records']:
        source_name = row.get('source') or '未知来源'
        item = sources.setdefault(source_name, {'count':0,'fields':collections.Counter(),'statuses':collections.Counter(),'methods':collections.Counter()})
        item['count'] += 1
        item['fields'].update(k for k,v in row.items() if v not in (None, '', [], {}))
        item['statuses'][row.get('transaction_status')] += 1
        item['methods'][row.get('payment_method')] += 1
    report = {'book_name':book['name'],'workspace':str(target),'summary':data['summary'],
              'sources':sources,'source_database_hashes':before,'source_unchanged':True}
    (target.parent / 'baseline-private.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'workspace':str(target),'summary':data['summary'],
                      'sources':{s:{'count':v['count'],'statuses':v['statuses'],'methods':v['methods']} for s,v in sources.items()},
                      'source_unchanged':True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
