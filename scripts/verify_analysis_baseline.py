"""Structural checks on an explicitly isolated real-book baseline; no network."""
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.service import App
from analysis.core import build
from analysis_baseline import digest_database


def main():
    parent=Path(sys.argv[1]).resolve()
    if not parent.is_relative_to((ROOT/'.local-data').resolve()) or not parent.name.startswith('analysis-baseline-'):
        raise RuntimeError('Only an isolated baseline directory is accepted.')
    baseline=json.loads((parent/'baseline-private.json').read_text(encoding='utf8'))
    app=App(parent/'workspace',ocr=False);data=app.analysis_data();store=app.analysis_store(data['book_id'])
    result=store.view(data)
    covered={rid for t in result['transactions'] for rid in t['source_ids']}
    assert covered=={r['book_record_id'] for r in data['records']}
    # Every known source field remains available with its original effective value.
    for tx in result['transactions']:
        for field,selection in tx['fields'].items():
            for row in tx['sources']:
                if row.get(field) not in (None,'','/','-'):
                    assert any(a['value']==row[field] and row['book_record_id'] in a['sources'] for a in selection['alternatives'])
    payment=[p for p in result['relations'] if p['kind']=='payment']
    consumed=Counter(rid for p in payment for rid in p['source_ids'])
    assert all(n==1 for n in consumed.values())
    shuffled={**data,'records':list(reversed(data['records']))}
    independent=build(shuffled)
    assert result['totals']==independent['totals']
    assert {p['id'] for p in result['relations']}=={p['id'] for p in independent['relations']}
    current={relative:digest_database(ROOT/'.local-data/workspace'/relative) for relative in baseline['source_database_hashes']}
    unchanged=current==baseline['source_database_hashes']
    report={'source_count':result['source_count'],'transaction_count':result['transaction_count'],
            'reading_summary':result['reading_summary'],'relations':dict(Counter(p['kind'] for p in result['relations'])),
            'candidate_count':len(result['candidates']),'truncated':result['truncated'],
            'all_source_records_retained':True,'all_merged_fields_retained':True,
            'payment_sources_not_double_allocated':True,'order_independent':True,
            'original_daily_databases_unchanged':unchanged,
            'validation_limit':'Structural and field-preservation verification; not independent manual ground truth or live AI accuracy.'}
    (parent/'analysis-validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))
    assert unchanged,'Daily data changed since baseline; compare user activity before using old baseline.'


if __name__=='__main__':main()
