"""Read an explicit exported snapshot; output only aggregate portability diagnostics.

Never opens the live workspace, calls AI, infers new source identities, or writes
financial decisions. Usage: python scripts/audit_rule_coverage.py snapshot.json
"""
import collections
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from analysis.core import bank, platform
from jiaowopay_ingest.profiles import BY_ID


def report(data):
    rows = data['records']
    profiles = collections.Counter(r.get('profile','missing') for r in rows)
    legacy = sum(bank(r) or platform(r) for r in rows)
    renamed = sum(bank({**r,'source':'Example Institution'}) or
                  platform({**r,'source':'Example Institution'}) for r in rows)
    roles = collections.Counter(BY_ID[r['profile']].role if r.get('profile') in BY_ID
                                else 'needs_file_confirmation' for r in rows)
    # A profile knows the source role, not the account/processor/cash-effect facts.
    return {'record_count':len(rows), 'profile_counts':dict(sorted(profiles.items())),
            'legacy_classified_rows':legacy, 'legacy_classified_after_label_change':renamed,
            'registry_roles':dict(sorted(roles.items())),
            'registry_roles_depend_on_display_label':False,
            'complete_new_evidence_contract_present':sum(isinstance(r.get('evidence_facts'),dict) for r in rows),
            'new_engine_run_on_legacy_records':False,
            'note':'Role coverage is not pairing accuracy; legacy exports lack the new evidence provenance contract.'}


def main():
    if len(sys.argv)!=2: raise SystemExit('Provide an explicit exported snapshot JSON path')
    path = Path(sys.argv[1])
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    result = report(json.loads(raw.decode('utf-8-sig')))
    result['snapshot_sha256'] = digest
    result['input_unchanged'] = hashlib.sha256(path.read_bytes()).hexdigest()==digest
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
