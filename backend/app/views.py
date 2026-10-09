"""Read models: source-record amounts, never personal expenditure inference."""
from collections import defaultdict
from jiaowopay_ingest.semantics import searchable
from decimal import Decimal
from jiaowopay_review.bookstore import export_book
from jiaowopay_review.store import ReviewStore, ReviewError


def overview(catalog):
    bid = catalog.context()['book_id']
    if not bid or not catalog.batches(bid):
        return {'status': 'empty', 'sources': [], 'range': None, 'personal_expense': None,
                'analysis_status': 'not_implemented', 'transaction_deduplication': False}
    data = export_book(catalog, bid, allow_partial=True)
    groups = {}
    dates = []
    defaulted = 0
    for row in data['records']:
        key = (row.get('source') or '未知来源', row.get('currency') or '未知币种')
        group = groups.setdefault(key, {'source': key[0], 'currency': key[1], 'count': 0,
            'in': Decimal(0), 'out': Decimal(0), 'none': Decimal(0), 'statuses': defaultdict(int)})
        group['count'] += 1
        group['statuses'][row.get('transaction_status') or 'unknown'] += 1
        direction = row.get('direction')
        if direction in {'in', 'out', 'none'} and row.get('amount') is not None:
            group[direction] += Decimal(row['amount'])
        date = row.get('transaction_at') or row.get('booking_at')
        if date:
            dates.append(date[:10])
    # Currency defaults are source warnings, also relevant after human review.
    for batch in catalog.batches(bid):
        try:
            store = ReviewStore(batch['bundle'], batch['states'])
            defaulted += sum(i['code'] == 'CURRENCY_DEFAULTED' for i in store.issues)
        except Exception:
            pass  # The authoritative export already marks unreadable batches.
    return {'status': 'partial' if data['partial'] else 'ready',
            'summary': data['summary'], 'batch_count': len(data['batches']),
            'sources': [{**g, **{k: format(g[k], 'f') for k in ('in', 'out', 'none')},
                         'statuses': dict(g['statuses'])} for g in groups.values()],
            'range': {'start': min(dates), 'end': max(dates)} if dates else None,
            'currency_default_count': defaulted,
            'unreadable_batch_ids': data['unreadable_batch_ids'],
            'blockers': data['blockers'], 'batches': data['batches'],
            'personal_expense': None, 'analysis_status': 'not_implemented',
            'transaction_deduplication': False,
            'basis': '已可用记录的来源金额；包含各交易状态，尚未去重或判断个人支出。'}


def records_page(catalog, params):
    value = lambda key, default='': params.get(key, [default])[0]
    try:
        page, size = int(value('page', '1')), int(value('page_size', '50'))
    except ValueError:
        raise ReviewError('页码格式无效。') from None
    if page < 1 or not 1 <= size <= 100:
        raise ReviewError('页码或每页数量超出范围。')
    bid = catalog.context()['book_id']
    if not bid:
        return {'records': [], 'total': 0, 'page': page, 'page_size': size, 'unreadable_batches': []}
    rows, unreadable = [], []
    query = value('q').strip().casefold()
    for batch in catalog.batches(bid):
        if value('batch_id') and batch['id'] != value('batch_id'):
            continue
        try:
            snap = ReviewStore(batch['bundle'], batch['states']).snapshot()
        except Exception:
            unreadable.append(batch['id'])
            continue
        for row in snap['records']:
            status = value('status')
            if status == 'pending' and not (row['review_status'] == 'pending' or row['page_review_pending']):
                continue
            if status == 'eligible' and not row['eligible_for_processing']:
                continue
            if status == 'excluded' and row['review_status'] != 'excluded':
                continue
            if value('source') and row.get('source') != value('source'):
                continue
            if query and query not in searchable(row):
                continue
            date = (row.get('transaction_at') or row.get('booking_at') or '')[:10]
            if value('start') and (not date or date < value('start')):
                continue
            if value('end') and (not date or date > value('end')):
                continue
            rows.append({**row, 'batch_id': batch['id'], 'batch_name': batch['name'],
                         'workspace_id': snap['workspace_id'], 'revision': snap['revision'],
                         'book_record_id': batch['id'] + ':' + row['record_id']})
    rows.sort(key=lambda r: (r.get('transaction_at') or r.get('booking_at') or '', r['book_record_id']), reverse=True)
    return {'records': rows[(page-1)*size:page*size], 'total': len(rows), 'page': page,
            'page_size': size, 'unreadable_batches': unreadable}
