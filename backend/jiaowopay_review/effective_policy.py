"""Versioned read overlay; never rewrite imports or pretend a policy is a user review."""
import copy
import json
from collections import defaultdict

from jiaowopay_ingest.mapping import Mapping, document_profile
from jiaowopay_ingest.model import Row, issue
from jiaowopay_ingest.normalize import normalize
from jiaowopay_ingest.review import reason_text
from jiaowopay_ingest.storage import resolve_evidence
from jiaowopay_ocr.policy import VERSION as OCR_VERSION, assess, auxiliary

VERSION = 'effective-reading-2026-10-09-v2'


def apply_reading_policy(originals, raw_rows, source_issues, source_tasks, bundle):
    records = copy.deepcopy(originals)
    issues = copy.deepcopy(source_issues)
    tasks = copy.deepcopy(source_tasks)
    context = defaultdict(list)
    for raw in raw_rows.values():
        if raw.get('kind') == 'pdf_text':
            context[raw['file_id']].extend(str(v) for v in raw['values'])
    context = {fid: '\n'.join(parts) for fid, parts in context.items()}
    evidence_cache = {}
    for rid, rec in records.items():
        original = originals[rid]
        updated = False
        if rec.get('kind') == 'pdf_table' and rec.get('profile') == 'generic.v1':
            mapping = document_profile(Mapping(rec.get('mapping_columns', {}), rec.get('raw_headers', []), 0), context.get(rec['file_id'], ''))
            raw = raw_rows.get(rec.get('raw_id'))
            if raw and mapping.profile == 'ccb.account_activity.pdf.v1':
                row = Row(raw['values'], raw['row'], cell_types=raw.get('cell_types', []),
                          formulas={int(k): v for k, v in raw.get('formulas', {}).items()})
                normalized, problems = normalize(row, mapping)
                rec.update(normalized)
                issues = [i for i in issues if i.get('record_id') != rid]
                issues.extend({**i, 'record_id':rid, 'file_id':rec['file_id'], 'raw_id':rec['raw_id']} for i in problems)
                updated = True
        if rec.get('extraction_method') == 'local_ocr' and rec.get('ocr_policy') == 'ocr-review-2-cny-default':
            path = rec.get('ocr_evidence', {}).get('ocr_json')
            if path:
                if path not in evidence_cache:
                    evidence_cache[path] = json.loads(resolve_evidence(bundle, path).read_text(encoding='utf8'))
                evidence = evidence_cache[path]
                index = rec['row'] - 1
                gates = assess(evidence, index, rec)
                rec.update(auxiliary(evidence, index), ocr_policy=OCR_VERSION)
                issues = [i for i in issues if not (i.get('record_id') == rid and i['code'] == 'OCR_RECORD_REVIEW')]
                if gates:
                    issues.append(issue('OCR_RECORD_REVIEW', '金额、方向、日期或整页完整性的识别证据需要核对。',
                                        record_id=rid, file_id=rec['file_id'], raw_id=rec['raw_id'], page=rec.get('page'), reason_codes=gates))
                rec['parse_status'] = 'needs_review' if any(i.get('record_id') == rid and i['severity'] == 'error' for i in issues) else 'ready'
                for task in tasks.values():
                    if task.get('record_id') == rid:
                        task['original_reason_codes'] = task.get('reason_codes', [])
                        task['reason_codes'] = gates
                        task['reason_messages'] = [reason_text(code) for code in gates]
                        task['policy_version'] = OCR_VERSION
                updated = True
        from jiaowopay_ingest.semantics import enrich
        raw = raw_rows.get(rec.get('raw_id'))
        if raw:
            enrich(rec, rec.get('raw_headers', []), rec.get('mapping_columns', {}), raw['values'])
        if updated:
            rec['policy_adjustment'] = {'version':VERSION, 'original_parse_status':original['parse_status'],
                                        'original_direction':original.get('direction'), 'original_profile':original.get('profile'),
                                        'original_ocr_policy':original.get('ocr_policy')}
    return records, issues, tasks
