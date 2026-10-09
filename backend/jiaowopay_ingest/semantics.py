"""Descriptive coverage, independent of financial eligibility and human decisions."""
from .model import raw_value
from .mapping import norm

VERSION = 'source-semantics-1'
LABELS = {'memo': '附言 / 备注', 'payment_method': '支付方式 / 渠道',
          'trade_type': '交易类型', 'original_category': '来源分类',
          'merchant_order_no': '商家订单号', 'counterparty_account': '对方账号',
          'counterparty_bank': '对方开户行', 'counterparty_combined': '对方账号与户名'}


def meaningful(value):
    return value is not None and str(value).strip().strip('-—_/＊* ') != ''


def enrich(record, headers, columns, values, preserve=False):
    """Keep each source cell, including unmapped cells; never infer identity from masks."""
    fields = []
    for index, value in enumerate(values):
        header = headers[index] if index < len(headers) else f'第 {index + 1} 列'
        targets = [key for key, col in columns.items() if col == index]
        retained = str(header).strip() in {'序号', '钞汇', '对方账号与户名'}
        fields.append({'index': index, 'header': header, 'value': raw_value(value),
                       'targets': targets, 'usage': 'retained' if retained else 'mapped' if targets else 'unmapped'})
    record['source_fields'] = fields
    record['unmapped_headers'] = list(dict.fromkeys(f['header'] for f in fields
        if f['usage'] == 'unmapped' and meaningful(f['value'])))
    record['semantic_policy'] = VERSION
    hints = {}
    # Only a recognized CCB format gives this overloaded header this meaning.
    # Explicit user mappings and human edits always take precedence.
    profile = record.get('profile', '')
    memo_index = columns.get('memo')
    memo_header = headers[memo_index] if isinstance(memo_index, int) and memo_index < len(headers) else ''
    memo = record.get('memo')
    if record.get('mapping_method') != 'user_confirmed' and meaningful(memo):
        if profile.startswith('ccb.account_activity.') and norm(memo_header) == norm('交易地点/附言'):
            hints['counterparty'] = {'value': memo, 'header': memo_header,
                                      'label': '附言中的对方 / 渠道线索'}
        if profile.startswith('boc.account_activity.'):
            hints['description'] = {'value': memo, 'header': memo_header, 'label': '来源附言摘要'}
    record['semantic_hints'] = hints
    derived = dict(record.get('derived_fields', {}))
    for key, hint in hints.items():
        if not preserve and not meaningful(record.get(key)):
            record[key] = hint['value']
            derived[key] = hint
    record['derived_fields'] = derived
    return record


def searchable(record):
    """All retained source context is searchable, even when not yet categorized."""
    values = [record.get(k) for k in ('filename', 'counterparty', 'description', 'amount',
              'source_record_id', *LABELS)]
    values.extend(f['value'] for f in record.get('source_fields', []))
    return ' '.join(str(v) for v in values if v is not None).casefold()
