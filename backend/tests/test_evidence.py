import copy
import itertools
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis.evidence import Fact, TimeRange, Observation, Coverage, Policy, assess_pair, propose_groups
from jiaowopay_ingest.mapping import Mapping, _profile, document_profile
from jiaowopay_ingest.profiles import PROFILES, matching_profiles


def fact(value, **kw):
    return Fact(value, references=('test-source:field',), origin='source', **kw)


def observation(rid, role, *, amount='17.30', direction='out', kind='payment', precision=None, **facts):
    day = role=='account_ledger'
    values = dict(role=role, amount=amount, currency='CNY', direction=direction,
                  event_kind=kind, cash_effect=True, funding_account='confirmed-account-A',
                  processor='processor-X', time=TimeRange.from_value(
                      '2026-01-02' if day else '2026-01-02T11:43:25',
                      precision or ('day' if day else 'second'), basis='local-clock-X'))
    return Observation(rid, {**{k:fact(v) for k,v in values.items()}, **facts})


COMPLETE = Coverage(True, 'all-effective-records:synthetic-v1')


class EvidenceTests(unittest.TestCase):
    def pair(self):
        return observation('b','account_ledger'), observation('p','payment_platform')

    def test_display_names_do_not_change_evidence(self):
        a,b = self.pair()
        expected = assess_pair(a,b)
        for label in ('建设银行','Example Credit Union','新机构甲','', '支付机构乙'):
            self.assertEqual(assess_pair(replace(a,label=label),replace(b,label=label)),expected)

    def test_defaults_ai_missing_provenance_never_become_facts(self):
        a,b = self.pair()
        for value in (Fact('CNY','default',('global-default',)),
                      Fact('CNY','ai',('response-1',)), Fact('CNY','source')):
            row = replace(a,facts={**a.facts,'currency':value})
            self.assertEqual(assess_pair(row,b)['state'],'needs_evidence')

    def test_hard_conflicts_override_reference(self):
        a,b = self.pair()
        a = replace(a,facts={**a.facts,'references':fact((('processor-X:order','abc'),))})
        for key,value in [('currency','USD'),('direction','in'),('amount','18.30'),
                          ('event_kind','refund'),('funding_account','account-B'),
                          ('processor','processor-Y'),('cash_effect',False)]:
            bb = replace(b,facts={**b.facts,key:fact(value),'references':a.fact('references')})
            with self.subTest(key=key): self.assertEqual(assess_pair(a,bb)['state'],'rejected')

    def test_unknown_event_not_inferred_from_amount_or_channel(self):
        a,b = self.pair()
        bb = replace(b,facts={**b.facts,'event_kind':Fact()})
        self.assertIn('event_kind_unknown',assess_pair(a,bb)['missing'])

    def test_reference_namespace_and_account_completeness(self):
        a,b = self.pair()
        a = replace(a,facts={**a.facts,'funding_account':Fact(),'references':fact((('bank:own','123456'),))})
        b = replace(b,facts={**b.facts,'references':fact((('platform:order','123456'),))})
        self.assertEqual(assess_pair(a,b)['state'],'needs_evidence')
        b = replace(b,facts={**b.facts,'references':a.fact('references')})
        self.assertEqual(assess_pair(a,b)['state'],'supported')

    def test_missing_time_and_booking_time_are_not_midnight(self):
        a,b = self.pair()
        self.assertEqual(assess_pair(a,b)['state'],'supported')
        for time in (Fact(),fact(replace(a.fact('time').value,role='booking')),
                     fact(replace(a.fact('time').value,basis='another-clock'))):
            self.assertEqual(assess_pair(replace(a,facts={**a.facts,'time':time}),b)['state'],'needs_evidence')
        self.assertEqual((a.fact('time').value.end-a.fact('time').value.start).total_seconds(),86400)

    def test_time_tolerance_is_explicit_not_a_hidden_bank_rule(self):
        a,b = self.pair()
        a = replace(a,facts={**a.facts,'time':fact(TimeRange.from_value('2026-01-02T11:45:25','second',basis='local-clock-X'))})
        self.assertEqual(assess_pair(a,b)['state'],'rejected')
        self.assertEqual(assess_pair(a,b,Policy(clock_skew_seconds=120))['state'],'supported')

    def test_exact_money_does_not_round_or_accept_float(self):
        for amount in ('1.001','123456.789','42','42.00'):
            a,b = self.pair()
            a = replace(a,facts={**a.facts,'amount':fact(amount)})
            b = replace(b,facts={**b.facts,'amount':fact(amount)})
            self.assertEqual(assess_pair(a,b)['state'],'supported')
        a,b = self.pair()
        for amount in (17.30, True, 'NaN', 'Infinity', '-17.30','0'):
            self.assertEqual(assess_pair(replace(a,facts={**a.facts,'amount':fact(amount)}),b)['state'],'rejected')

    def test_two_purchases_and_refund_are_three_events_not_one(self):
        rows = [observation(rid,role,amount='21.00',direction=direction,kind=kind)
                for rid,role,direction,kind in (
                    ('b1','account_ledger','out','payment'),('b2','account_ledger','out','payment'),
                    ('p1','payment_platform','out','payment'),('p2','payment_platform','out','payment'),
                    ('br','account_ledger','in','refund'),('pr','payment_platform','in','refund'))]
        original = copy.deepcopy(rows)
        result = propose_groups(rows,COMPLETE)
        outgoing = next(g for g in result['groups'] if g['direction']=='out')
        incoming = next(g for g in result['groups'] if g['direction']=='in')
        self.assertEqual(outgoing['retained_event_count_if_confirmed'],2)
        self.assertEqual(outgoing['duplicate_amount_if_confirmed'],'42.00')
        self.assertEqual(outgoing['assignment'],'unresolved')
        self.assertEqual(outgoing['forced_pairs'],[])
        self.assertEqual(outgoing['field_attribution'],'group_only')
        self.assertEqual(incoming['retained_event_count_if_confirmed'],1)
        self.assertEqual(incoming['duplicate_amount_if_confirmed'],'21.00')
        self.assertTrue(all(not g['automatic'] for g in result['groups']))
        self.assertFalse(result['writes_book'])
        self.assertEqual(rows,original)

    def test_input_order_never_resolves_equal_payment_ties(self):
        rows = [observation('b1','account_ledger'),observation('b2','account_ledger'),
                observation('p1','payment_platform'),observation('p2','payment_platform')]
        expected = propose_groups(rows,COMPLETE)['groups']
        for ordering in itertools.permutations(rows):
            self.assertEqual(propose_groups(ordering,COMPLETE)['groups'],expected)

    def test_unbalanced_and_incomplete_groups_do_not_reduce_totals(self):
        a,b = self.pair()
        cases = [(list((a,b)),Coverage()),([a,b,replace(a,id='b2')],COMPLETE),
                 ([a,b],replace(COMPLETE,blocked_records=frozenset({'b'})))]
        for rows,coverage in cases:
            g = propose_groups(rows,coverage)['groups'][0]
            self.assertEqual(g['state'],'blocked')
            self.assertIsNone(g['duplicate_amount_if_confirmed'])

    def test_unknown_external_competitor_blocks_promotion(self):
        a,b = self.pair()
        c = replace(b,id='p2',facts={**b.facts,'funding_account':Fact()})
        g = propose_groups([a,b,c],COMPLETE)['groups'][0]
        self.assertIn('unresolved_competitor',g['reasons'])

    def test_truncation_is_an_error_not_a_unique_pair(self):
        a,b = self.pair()
        with self.assertRaises(ValueError): propose_groups([a,b],COMPLETE,Policy(max_observations=1))
        with self.assertRaises(ValueError): propose_groups([a,a],COMPLETE)

    def test_conflicting_evidence_requires_resolution(self):
        a,b = self.pair()
        a = replace(a,facts={**a.facts,'direction':fact('out',conflict=True)})
        self.assertIn('direction_evidence_conflict',assess_pair(a,b)['conflicts'])


class AdapterRegistryTests(unittest.TestCase):
    def test_all_five_legacy_profiles_preserve_mapping_contract(self):
        self.assertEqual(len(PROFILES),5)
        for profile in PROFILES:
            headers = sorted(profile.headers)
            mapping = Mapping({},headers,0)
            got = document_profile(mapping,profile.document_marker) if profile.document_marker else _profile(mapping)
            self.assertEqual((got.profile,got.source,got.signed),(profile.id,profile.label,profile.signed))
            if not profile.document_marker: self.assertEqual(got.currency,profile.currency)
            self.assertEqual(len(matching_profiles(headers,profile.document_marker)),1)

    def test_schema_drift_does_not_inherit_a_bank_contract(self):
        for profile in PROFILES:
            headers = sorted(profile.headers)[1:]
            self.assertNotIn(profile,matching_profiles(headers,profile.document_marker))
        p = next(p for p in PROFILES if p.document_marker)
        self.assertNotIn(p,matching_profiles(p.headers,'unrelated document heading'))

    def test_manual_mapping_has_priority_over_document_profile(self):
        p = next(p for p in PROFILES if p.document_marker)
        mapping = Mapping({},list(p.headers),0,confirmed=True,source='user chosen')
        self.assertEqual(document_profile(mapping,p.document_marker).source,'user chosen')


if __name__ == '__main__': unittest.main()
