"""Versioned gates, NOT a probability of OCR correctness or settlement."""
import math
import re
from decimal import Decimal

from .table import clean

VERSION = 'ocr-review-3-critical-fields'
MIN_SCORE = .98
CRITICAL = {'transaction_at', 'amount', 'direction'}
BALANCE_REASONS = {'OCR_VIEW_CONFLICT:balance', 'VIEW_LOST_FIELD:balance',
                   'RECOVERED_FROM_DERIVED_VIEWS:balance', 'UNRESOLVED_FIELD:balance',
                   'BALANCE_UNREADABLE', 'BALANCE_CHAIN_CONFLICT'}
CURRENCIES = {'人民币':'CNY','CNY':'CNY','RMB':'CNY','美元':'USD','USD':'USD',
              '欧元':'EUR','EUR':'EUR','港币':'HKD','港元':'HKD','HKD':'HKD','日元':'JPY','JPY':'JPY'}


def currency(row):
    return CURRENCIES.get(clean(row.get('raw_fields',{}).get('currency','')).upper())


def printed_count(raw, rows):
    if not rows:
        return None
    bottom = rows[-1]['source_geometry']['date_y']
    parts=[]
    for box,text in zip(raw.get('boxes') or [],raw.get('texts') or []):
        if min(p[1] for p in box)>bottom:
            parts.append((min(p[1] for p in box),min(p[0] for p in box),text))
    text = clean(' '.join(t for _,_,t in sorted(parts)))
    counts = {int(n) for n in re.findall(r'本页交易(?:笔数|数量)[:：]?(\d+)',text)}
    return next(iter(counts)) if len(counts)==1 else None


def page_gate(result):
    rows=result.get('observations',{}).get('original',[])
    count=printed_count(result.get('raw_views',{}).get('original',{}),rows)
    reasons=list(result.get('proposal_issues',[]))
    if count is None:
        reasons.append('OCR_PAGE_COUNT_UNVERIFIED')
    elif count != len(result.get('proposals',[])):
        reasons.append('OCR_PAGE_COUNT_MISMATCH')
    return list(dict.fromkeys(reasons)),count


def assess(result, index, normalized):
    """No changes/conflicts, complete fields, high scores, agreeing views/count.

    Optional text is retained but not advertised as independently verified.
    Missing page count keeps the page and its records pending for a human.
    """
    row=result['proposals'][index]
    original=result['observations']['original'][index]
    reasons,_=page_gate(result)
    reasons += [x for x in row['issues'] if x != 'OCR_REQUIRES_REVIEW' and x not in BALANCE_REASONS]
    if any(not c.get('fields') or CRITICAL.intersection(c['fields']) for c in row.get('changes', [])):
        reasons.append('OCR_DERIVED_VALUE_REQUIRES_REVIEW')
    # LOW_OCR_SCORE was aggregated over every token, including optional text.
    # Recheck critical field scores below instead of blocking on that aggregate.
    if set(original.get('issues', [])) - BALANCE_REASONS - {'LOW_OCR_SCORE'}:
        reasons.append('OCR_ORIGINAL_UNCERTAIN')
    if any(normalized.get(f) is None for f in ['transaction_at','amount','direction','currency']):
        reasons.append('OCR_REQUIRED_FIELD_MISSING')
    fields = ['date'] + (['signed'] if 'signed' in original['raw_fields'] else ['credit','debit'])
    for f in fields:
        if f in {'credit','debit'} and not clean(original['raw_fields'].get(f,'')):
            continue
        scores=original.get('field_scores',{}).get(f,[])
        if not scores or any(not math.isfinite(s) or s<MIN_SCORE for s in scores):
            reasons.append('OCR_SCORE_BELOW_THRESHOLD')
    # Require agreement for all critical fields, not just a high confidence score.
    for group in ['transaction_at','amount+direction']:
        evidence=row.get('field_evidence',{}).get(group,{})
        before=evidence.get('original',{})
        if not before or any(v is None for v in before.values()) or not any(
            name in {'dark_gray','dark_neutral'} and value==before for name,value in evidence.items()
        ):
            reasons.append('OCR_INSUFFICIENT_AGREEMENT')
    # Currency is normalized using the user-selected CNY default. Its OCR
    # score or cross-view uncertainty alone does not require review.
    return sorted(set(reasons))


def auxiliary(result, index):
    """Keep uncertain balances auditable without blocking transaction cashflows."""
    row = result['proposals'][index]
    original = result['observations']['original'][index]
    reasons = sorted((set(row.get('issues', [])) | set(original.get('issues', []))) & BALANCE_REASONS)
    scores = original.get('field_scores', {}).get('balance', [])
    if not scores or any(not math.isfinite(s) or s < MIN_SCORE for s in scores):
        reasons.append('BALANCE_SCORE_UNCERTAIN')
    evidence = row.get('field_evidence', {}).get('balance', {})
    before = evidence.get('original', {})
    if not before or any(v is None for v in before.values()) or not any(
            evidence.get(route) == before for route in ['dark_gray', 'dark_neutral']):
        reasons.append('BALANCE_AGREEMENT_UNCERTAIN')
    if any('balance' in c.get('fields', []) for c in row.get('changes', [])):
        reasons.append('BALANCE_DERIVED')
    return {'balance_reliable': not reasons, 'auxiliary_reason_codes': sorted(set(reasons))}
