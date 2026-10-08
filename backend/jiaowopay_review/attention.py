"""Locate review concerns in canonical fields and original columns, without editing data."""
import math
from collections import defaultdict

from jiaowopay_ingest.mapping import map_header
from jiaowopay_ocr.policy import MIN_SCORE

MONEY = ('amount', 'signed_amount', 'credit_amount', 'debit_amount')
GROUPS = {'transaction_at':('transaction_at',), 'amount+direction':('amount','direction')}
ISSUE_FIELDS = {'AMOUNT_MISSING':('amount',), 'AMOUNT_INVALID':('amount',),
                'AMOUNT_CONFLICT':('amount',), 'BOTH_DIRECTIONS':('amount','direction'),
                'SPLIT_AMOUNT_NEGATIVE':('amount','direction'),
                'DIRECTION_MISSING':('direction',), 'DIRECTION_UNKNOWN':('direction',),
                'DIRECTION_CONFLICT':('amount','direction'),
                'CURRENCY_CONFLICT':('currency','amount')}


def field_attention(record, original, issues, ocr=None):
    """Only unresolved row errors are highlighted; balance advisories stay quiet."""
    if record.get('review_status') != 'pending':
        return {'fields':{}, 'raw_columns':[], 'unlocalized':[]}
    fields = defaultdict(list)
    unlocalized = []

    def add(names, reason):
        for name in names:
            if reason not in fields[name]:
                fields[name].append(reason)

    for issue in issues:
        if issue.get('record_id') != record['record_id'] or issue.get('severity') != 'error':
            continue
        code, field = issue['code'], issue.get('field')
        if code == 'OCR_RECORD_REVIEW':
            if not ocr:
                unlocalized.append(issue['message'])
            continue
        names = ISSUE_FIELDS.get(code, ())
        if field in MONEY:
            names = tuple(set(names) | {'amount'})
        elif field:
            field = {'transaction_time':'transaction_at', 'booking_time':'booking_at'}.get(field,field)
            names = tuple(set(names) | {field})
        if code == 'DATE_MISSING':
            columns = original.get('mapping_columns', {})
            names = tuple(k for k in ['transaction_at','booking_at'] if k in columns)
        if names:
            add(names,issue['message'])
        else:
            unlocalized.append(issue['message'])

    if ocr:
        index=record['row']-1
        source=ocr['observations']['original'][index]
        proposal=ocr['proposals'][index]
        for group,names in GROUPS.items():
            evidence=proposal.get('field_evidence',{}).get(group,{})
            before=evidence.get('original',{})
            if not before or any(v is None for v in before.values()) or not any(evidence.get(v)==before for v in ['dark_gray','dark_neutral']):
                add(names,'原图与增强图未取得充分一致证据')
            if any(record.get(name) is None for name in names):
                add(names,'关键内容缺失或无法可靠读取')
            if any(set(change.get('fields',[])).intersection(names) for change in proposal.get('changes',[])):
                add(names,'内容经过增强识别恢复或出现冲突，请对照原图')
            if any(reason.endswith(':'+group) for reason in proposal.get('issues',[])):
                add(names,'不同识别路径存在缺失、恢复或冲突')
        checks={'date':('transaction_at',)}
        checks.update({'signed':('amount','direction')} if 'signed' in source['raw_fields'] else {'credit':('amount','direction'),'debit':('amount','direction')})
        for key,names in checks.items():
            if key in {'credit','debit'} and not str(source['raw_fields'].get(key,'')).strip():
                continue
            scores=source.get('field_scores',{}).get(key,[])
            if not scores or any(not math.isfinite(score) or score<MIN_SCORE for score in scores):
                add(names,'关键字段识别分数缺失或未达放行门槛')

    columns=original.get('mapping_columns', {})
    _,ambiguous=map_header(original.get('raw_headers', []))
    raw=defaultdict(lambda: {'fields':[], 'reasons':[]})
    for field,reasons in fields.items():
        keys = MONEY if field=='amount' else ('direction',) if field=='direction' and ('direction' in columns or 'direction' in ambiguous) else MONEY if field=='direction' else (field,)
        if field in {'transaction_at','booking_at'}:
            keys = (field, 'transaction_time' if field=='transaction_at' else 'booking_time')
        if field=='currency' and not ('currency' in columns or 'currency' in ambiguous):
            keys = MONEY  # Currency may be carried by a money token rather than a separate column.
        indices={columns[k] for k in keys if k in columns}
        indices.update(i for key in keys for i in ambiguous.get(key,[]))
        for index in indices:
            raw[index]['fields'].append(field)
            raw[index]['reasons']=list(dict.fromkeys(raw[index]['reasons']+reasons))
    return {'fields':dict(fields), 'raw_columns':[{'index':i, **raw[i]} for i in sorted(raw)],
            'unlocalized':list(dict.fromkeys(unlocalized))}
