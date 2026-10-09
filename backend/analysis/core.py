"""Exact-money rules, field provenance and deterministic transaction projection.

Reuses the old reconcile module's dependency/amount constraints as a design,
not its all-candidates-as-primary-workflow or personal-expense projection.
"""
import itertools
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from jiaowopay_ingest.model import digest
from jiaowopay_review.store import ReviewError

VERSION = 'flow-rules-1.3.0'
IMPLEMENTATION = digest(Path(__file__).read_bytes().hex())
MERGE_VERSION = 'field-union-1'
PLATFORMS = {'微信', '微信支付', '支付宝'}
FIELDS = ('transaction_at','booking_at','counterparty','description','memo','payment_method',
          'account','counterparty_account','counterparty_combined','counterparty_bank',
          'source_record_id','merchant_order_no','trade_type','original_category','transaction_status',
          'raw_status','balance','balance_reliable')
GENERIC = {'消费','转账','支付','付款','其他','未知','财付通','支付宝','微信支付','/','-','无'}
LIMIT = 1200


def rules_need_restart():
    return digest(Path(__file__).read_bytes().hex()) != IMPLEMENTATION


def money(value):
    try:
        if isinstance(value, (bool,float)):
            raise ValueError()
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation):
        raise ReviewError('金额必须是有限非负十进制数。') from None


def norm(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def identifier(value):
    value = str(value or '').strip()
    return value if len(value) >= 6 and not re.search(r'[*×]|x{3,}', value, re.I) else None


def platform(row):
    return row.get('source') in PLATFORMS


def bank(row):
    return '银行' in str(row.get('source') or '')


def time_value(row):
    field = 'transaction_at' if row.get('transaction_at') else 'booking_at'
    try:
        return datetime.fromisoformat(row[field]), row.get(field+'_precision', 'day')
    except (KeyError, ValueError, TypeError):
        return None, None


def window(a,b):
    ta,pa = time_value(a); tb,pb = time_value(b)
    if ta is None or tb is None:
        return False
    if pa in {'day','date',None} or pb in {'day','date',None}:
        return ta.date() == tb.date()
    return abs((ta-tb).total_seconds()) <= 300


def is_refund(row):
    if row.get('transaction_status') in {'void','pending'}:
        return False
    # A purchase's status "已退款" is NOT another receipt.
    return (row.get('direction') == 'in' and
            (row.get('trade_type') in {'退款','商户退款','网上快捷退款'} or
             (row.get('source') in {'微信','微信支付'} and bool(re.search(r'[-－]退款$',str(row.get('trade_type') or '')))) or
             row.get('description') in {'退款','消费退货','退货','消费退款'} or
             row.get('raw_status') == '退款成功')) or (
            row.get('source') == '支付宝' and row.get('direction') == 'none' and
            row.get('raw_status') == '退款成功' and row.get('transaction_status') == 'posted')


def flow_direction(row):
    return 'in' if is_refund(row) else row.get('direction')


def eligible(row):
    return row.get('transaction_status') not in {'void','pending'} and flow_direction(row) in {'in','out'}


def fingerprint(data):
    return digest(VERSION, IMPLEMENTATION, MERGE_VERSION, data['book_id'], data.get('partial'),
                  data.get('batches'), data.get('records'), data.get('blockers'))


def merge(rows):
    """Union effective source fields; selected display values never erase alternatives."""
    rows = sorted(rows, key=lambda r:r['book_record_id'])
    values = {}
    for field in FIELDS:
        entries = []
        for row in rows:
            value = row.get(field)
            if value in (None,'','/','-'):
                continue
            same = next((v for v in entries if v['value'] == value), None)
            if same:
                same['sources'].append(row['book_record_id'])
            else:
                entries.append({'value':value,'sources':[row['book_record_id']]})
        def rank(entry):
            r = next(r for r in rows if r['book_record_id'] == entry['sources'][0])
            value = entry['value']
            semantic = int(field in {'counterparty','description','transaction_at'} and platform(r))
            specific = int(str(value) not in GENERIC)
            reliable = int(not (field=='balance' and r.get('balance_reliable') is False))
            # Effective human corrections already reside in each source row.
            return (reliable, specific, semantic, entry['sources'][0])
        entries.sort(key=rank, reverse=True)
        values[field] = {'selected':entries[0]['value'] if entries else None,
                         'selected_sources':entries[0]['sources'] if entries else [],
                         'alternatives':entries, 'multiple_values':len(entries)>1,
                         'policy':MERGE_VERSION}
    # Account balances and identifiers are source attributes, never a merged balance/id.
    record = {k:v['selected'] for k,v in values.items() if k not in {'balance','account','source_record_id','merchant_order_no','counterparty_account'}}
    anchor = rows[0]
    record.update(id=digest('transaction', [r['book_record_id'] for r in rows]),
                  amount=anchor['amount'],currency=anchor['currency'],direction=flow_direction(anchor),
                  refund=any(is_refund(r) for r in rows),source_ids=[r['book_record_id'] for r in rows],
                  fields=values,sources=rows)
    return record


def proposal(kind, left, rights, evidence, auto=False):
    ids = [left['id'], *sorted(r['id'] for r in rights)]
    return {'id':digest(kind,ids),'kind':kind,'left':left['id'],
            'rights':sorted(r['id'] for r in rights),'evidence':evidence,'automatic':auto,
            'amount':left['amount'],'currency':left['currency'],
            'source_ids':sorted({s for r in [left,*rights] for s in r['source_ids']})}


def row_of(tx):
    return tx['sources'][0]


def account_conflict(a,b):
    tail=re.search(r'[（(](\d{4})[）)]',str(b.get('payment_method') or ''))
    account=re.sub(r'\D','',str(a.get('account') or ''))
    return bool(tail and len(account)>=4 and not account.endswith(tail[1]))


def channel_match(a,b):
    method = norm(b.get('payment_method'))
    if account_conflict(a,b):return False
    return bool(a.get('source') and norm(a['source']) in method and '&' not in str(b.get('payment_method') or ''))


def processor_match(a,b):
    """Independent bank-side channel evidence; never infer it from amount alone."""
    text=' '.join(str(a.get(k) or '') for k in ('counterparty','memo','description'))
    wx=bool(re.search(r'财付通|微信',text));ali='支付宝' in text
    return (wx and not ali and b.get('source') in {'微信','微信支付'}) or (ali and not wx and b.get('source')=='支付宝')


def unique_channel_evidence(a,b):
    # Transfer/refund semantics need separate evidence; a generic channel cannot
    # identify the original transfer or refund target.
    text=' '.join(str(r.get(k) or '') for r in (a,b) for k in ('description','memo','trade_type'))
    return (a.get('direction')=='out' and b.get('direction')=='out' and
            not re.search(r'转账|红包|提现|充值|还款|借款|退款|退货',text) and
            channel_match(a,b) and processor_match(a,b) and window(a,b))


def wechat_transfer_evidence(a,b):
    """Bank business label and WeChat recipient are complementary source fields."""
    if b.get('source') not in {'微信','微信支付'} or a.get('direction')!='out' or b.get('direction')!='out':
        return False
    kind=norm(b.get('trade_type') or b.get('description'))
    kinds={'转账':'转账','微信转账':'转账','群收款':'群收款','微信群收款':'群收款',
           '微信红包':'红包','微信红包群红包':'红包','微信红包单发':'红包','微信红包单发红包':'红包'}
    kind=kinds.get(kind)
    if not kind:return False
    narrative=norm(' '.join(str(a.get(k) or '') for k in ('counterparty','memo')))
    text=narrative or norm(a.get('description'))
    # Some banks call card-funded WeChat transfers "充值" in their generic
    # summary. The explicit narrative is the business evidence, not that summary.
    if re.search(r'退款|退货|退回|提现',text+norm(a.get('description'))):return False
    if '充值' in text:return False
    if kind!='红包' and '红包' in text:return False
    if kind=='红包' and ('红包' not in text or '转账' in text or '群收款' in text):return False
    if kind=='转账' and ('转账' not in text or '群收款' in text):return False
    if kind=='群收款' and ('群收款' not in text or '转账' in text):return False
    return channel_match(a,b) and processor_match(a,b) and window(a,b)


def merchant_match(a,b):
    other = norm(b.get('counterparty'))
    if not other or '*' in str(b.get('counterparty')) or b.get('counterparty') in GENERIC:
        return False
    for field in ('counterparty','description','memo','counterparty_combined'):
        raw = str(a.get(field) or '').strip()
        for prefix in ('财付通-微信支付-','财付通－微信支付－','财付通-','财付通－','支付宝-支付宝外部商户-','支付宝-','支付宝－'):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        if norm(raw) == other:
            return True
    return False


def reference_match(a,b):
    # Bank narrative explicitly carries a platform reference; never equate own bank IDs.
    references = [identifier(b.get(k)) for k in ('source_record_id','merchant_order_no')]
    tokens = re.findall(r'[A-Za-z0-9_-]{6,}', ' '.join(str(a.get(k) or '') for k in ('memo','description')))
    return any(ref and ref in tokens for ref in references)


def source_duplicates(records):
    groups = defaultdict(list)
    for row in records:
        native = identifier(row.get('source_record_id'))
        key = (row.get('source'), row.get('account'), native, row.get('direction'),
               row.get('amount'),row.get('currency'),row.get('transaction_at'),row.get('booking_at'),
               row.get('transaction_status'),row.get('raw_status'))
        if not native:
            # Full semantic equality + balance + source, across exports only.
            if not row.get('balance') or row.get('source') in (None,'未知来源'):
                continue
            key += tuple(str(row.get(k) or '') for k in FIELDS)
        groups[key].append(row)
    result=[]
    for group in groups.values():
        if len(group)<2:
            continue
        files=Counter((r.get('batch_id'),r.get('file_id')) for r in group)
        if len(files)<2 or max(files.values())>1:
            continue  # Preserve multiplicity of identical legitimate payments.
        result.append(group)
    return result


def build(data, decisions=None):
    decisions = decisions or {}
    records = sorted(data.get('records',[]),key=lambda r:r['book_record_id'])
    current_hash = fingerprint(data)
    rules=[]; transactions=[]; used=set(); pending=[]; stale=[]
    def decision(p):
        d=decisions.get(p['id'])
        if not d:
            # Adding an overlapping export changes group IDs, not the user's intent.
            inherited=[v for v in decisions.values() if v.get('action') in {'reject','defer'} and
                       v.get('proposal',{}).get('kind')==p['kind'] and
                       set(v.get('proposal',{}).get('source_ids',[])) and
                       set(v['proposal']['source_ids']).issubset(p['source_ids'])]
            if inherited:d=max(inherited,key=lambda v:v.get('revision',0))
        if d and d['action'] in {'reject','defer'}:
            return d['action']  # User intent survives rule reruns and later input changes.
        if d and d.get('dependencies') != {r['book_record_id']:digest(r) for r in records if r['book_record_id'] in p['source_ids']}:
            if p['id'] not in stale:stale.append(p['id'])
            return 'stale'
        return d['action'] if d else None
    for group in source_duplicates(records):
        left,*right=[merge([r]) for r in group]
        p=proposal('duplicate',left,right,['同来源跨文件稳定标识／完整字段一致；币种、方向、金额、日期及状态一致'],True)
        choice=decision(p)
        if choice in {'reject','defer','stale'}:
            p['decision']=choice;pending.append(p);continue
        p['decision']='accepted' if choice=='accept' else 'automatic';rules.append(p)
        transactions.append(merge(group));used.update(r['book_record_id'] for r in group)
    transactions.extend(merge([r]) for r in records if r['book_record_id'] not in used)
    banks=[t for t in transactions if bank(row_of(t)) and eligible(row_of(t))]
    orders=[t for t in transactions if platform(row_of(t)) and eligible(row_of(t))]
    candidates=[];truncated=False
    for a in banks:
        ar=row_of(a)
        pool=[b for b in orders if a['currency']==b['currency'] and a['direction']==b['direction'] and
              not account_conflict(ar,row_of(b)) and
              (reference_match(ar,row_of(b)) or (channel_match(ar,row_of(b)) and window(ar,row_of(b))))]
        for b in pool:
            if money(a['amount']) != money(b['amount']):continue
            ref=reference_match(ar,row_of(b));merchant=merchant_match(ar,row_of(b))
            evidence=['币种、方向、金额一致']
            evidence+=['银行原文明确引用平台订单标识'] if ref else ['平台支付方式指向该银行，时间范围相容']
            if merchant:evidence.append('具体商户字段一致')
            channel=unique_channel_evidence(ar,row_of(b))
            if channel:evidence.append('银行与平台双向渠道一致；金额日期相容，须双方唯一后采用')
            transfer=wechat_transfer_evidence(ar,row_of(b))
            if transfer:evidence.append('微信转账／群收款／红包类型与银行渠道一致；好友名是收款对象，双方唯一时自动采用')
            refund_mirror=(is_refund(ar) and is_refund(row_of(b)) and channel_match(ar,row_of(b)) and processor_match(ar,row_of(b)) and window(ar,row_of(b)))
            if refund_mirror:evidence.append('双方均明确为退款入账，双向渠道一致；唯一时合并重复入账，原付款另行保留')
            p=proposal('payment',a,[b],evidence,ref or merchant or channel or transfer or refund_mirror)
            if not p['automatic']:
                p['review_reason']='收款性质或业务类型依据不足，不能仅凭同额同日认定为重复入账。' if a['direction']=='in' else '渠道相容，但交易类型或对象证据尚未满足自动采用条件。'
            candidates.append(p)
        if a['direction']=='out':
            if len(pool)>12:
                truncated=True;continue
            small=[b for b in pool if money(b['amount'])<money(a['amount'])]
            for size in range(2,min(4,len(small))+1):
                for group in itertools.combinations(small,size):
                    if len({row_of(b).get('source') for b in group})!=1:continue
                    if sum((money(b['amount']) for b in group),Decimal(0))!=money(a['amount']):continue
                    candidates.append(proposal('payment',a,list(group),['同平台订单合计等于银行金额；渠道与时间相容'],
                                               all(reference_match(ar,row_of(b)) for b in group)))
        if len(candidates)>LIMIT:
            truncated=True;break
    if len(candidates)>LIMIT:candidates=candidates[:LIMIT]
    # Unsupported subset sums must not veto a unique single payment. Proven
    # combined payments still compete; never resolve repeated equal payments by order.
    counts=Counter(tid for p in candidates if len(p['rights'])==1 or p['automatic']
                   for tid in [p['left'],*p['rights']])
    by_id={t['id']:t for t in transactions};consumed=set();payment=[];accepted=[]
    # Explicit human accepts first, but cannot violate shared amount constraints.
    candidates.sort(key=lambda p:(decisions.get(p['id'],{}).get('action')!='accept',p['id']))
    for p in candidates:
        choice=decision(p)
        endpoints={p['left'],*p['rights']}
        if endpoints&consumed:
            p['decision']='conflict';pending.append(p);continue
        unique=all(counts[x]==1 for x in endpoints) and not truncated
        if choice=='accept' or (not choice and p['automatic'] and unique):
            p['decision']='accepted' if choice else 'automatic';accepted.append(p);consumed.update(endpoints)
            anchor=by_id[p['left']]
            for oid in p['rights']:
                order=by_id[oid];tx=merge(order['sources']+anchor['sources'])
                tx.update(amount=order['amount'],direction=order['direction'],currency=order['currency'],
                          refund=order['refund'],id=digest('payment-event',order['id'],anchor['id']),
                          allocation={'bank_source_ids':anchor['source_ids'],'amount':order['amount']})
                payment.append(tx)
        else:
            p['decision']=choice or 'suggested'
            p['competing_candidates']=max(0,max((counts[x]-1 for x in endpoints),default=0))
            if not unique:p['review_reason']='存在同额竞争记录，无法唯一确定对应关系。' if not truncated else '候选检索不完整，自动采用已暂停。'
            if not unique:p['evidence'].append('存在竞争匹配或检索范围未完整覆盖，未自动采用')
            pending.append(p)
    for p in pending:
        if p['kind']=='payment' and p['decision']=='suggested' and {p['left'],*p['rights']}&consumed:
            p['decision']='conflict'
    rules.extend(accepted)
    transactions=[t for t in transactions if t['id'] not in consumed]+payment
    # Refund allocations operate on deduplicated transactions and never remove either flow.
    refunds=[t for t in transactions if t['refund']]
    purchases=[t for t in transactions if t['direction']=='out' and eligible(row_of(t))]
    refund_candidates=[]
    for credit in refunds:
        for expense in purchases:
            if credit['currency']!=expense['currency'] or money(credit['amount'])>money(expense['amount']):continue
            ct,_=time_value(credit);et,_=time_value(expense)
            if ct is None or et is None or ct<et:continue
            shared=False
            for c in credit['sources']:
                for e in expense['sources']:
                    if c.get('source') != e.get('source'):continue
                    shared |= any(identifier(c.get(k)) and c.get(k)==e.get(k) for k in ('source_record_id','merchant_order_no'))
            if shared:
                refund_candidates.append(proposal('refund',credit,[expense],['同来源业务订单标识明确关联；到账不早于付款；退款不超过付款'],True))
    credit_counts=Counter(p['left'] for p in refund_candidates)
    totals=defaultdict(lambda:Decimal(0));remaining=defaultdict(lambda:Decimal(0))
    tx_index={t['id']:t for t in transactions}
    for p in refund_candidates:totals[p['rights'][0]]+=money(p['amount'])
    links=[]
    for p in sorted(refund_candidates,key=lambda p:p['id']):
        choice=decision(p);expense=p['rights'][0]
        fits=totals[expense]<=money(tx_index[expense]['amount'])
        valid=remaining[expense]+money(p['amount'])<=money(tx_index[expense]['amount']) and not any(q['left']==p['left'] for q in links)
        if valid and (choice=='accept' or (not choice and credit_counts[p['left']]==1 and fits)):
            p['decision']='accepted' if choice else 'automatic';links.append(p);remaining[expense]+=money(p['amount'])
        else:
            p['decision']=choice or 'suggested';pending.append(p)
    rules.extend(links)
    totals={}
    for t in transactions:
        t['included']=eligible(row_of(t))
        t['refund_linked']=any(p['left']==t['id'] for p in links)
        if not t['included']:continue
        total=totals.setdefault(t['currency'],{'in':Decimal(0),'out':Decimal(0),'refund':Decimal(0)})
        total[t['direction']]+=money(t['amount'])
        if t['refund']:total['refund']+=money(t['amount'])
    rows_index={r['book_record_id']:r for r in records}
    for p in rules+pending:
        p['dependencies']={rid:digest(rows_index[rid]) for rid in p['source_ids']}
    live_ids={p['id'] for p in rules+pending}
    for pid,d in decisions.items():
        if pid not in live_ids and d['action']=='accept':stale.append(pid)
    return {'schema_version':'flow-analysis-1','policy':VERSION,'implementation':IMPLEMENTATION,'field_policy':MERGE_VERSION,
            'book_id':data['book_id'],'fingerprint':current_hash,'partial':data.get('partial',False),
            'reading_summary':data.get('summary',{}),'blockers':data.get('blockers',[]),
            'transactions':sorted(transactions,key=lambda t:(t.get('transaction_at') or t.get('booking_at') or '',t['id']),reverse=True),
            'relations':rules,'candidates':pending,'truncated':truncated,'stale_decisions':sorted(set(stale)),
            'source_count':len(records),'transaction_count':len(transactions),
            'totals':{c:{k:str(v) for k,v in total.items()} for c,total in totals.items()},
            'basis':'去重后的已覆盖流水；未决配对可能仍含重复，不是生活支出。'}
