"""Source-independent reconciliation prototype. Proposals only; never book writes.

Input facts must come from a versioned adapter or explicit user confirmation.
Legacy display strings, AI suggestions and defaults are NOT facts. No merchant,
bank, platform name, row order or balance is interpreted inside this module.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
import re

VERSION = 'evidence-prototype-1'
TRUSTED_ORIGINS = frozenset({'source', 'confirmed', 'adapter'})


@dataclass(frozen=True)
class Fact:
    value: object = None
    origin: str = 'unknown'
    references: tuple[str, ...] = ()
    conflict: bool = False

    @property
    def known(self):
        return (self.value is not None and self.value != '' and not self.conflict
                and self.origin in TRUSTED_ORIGINS and bool(self.references))


@dataclass(frozen=True)
class TimeRange:
    start: datetime
    end: datetime  # Exclusive; a day is a full interval, never midnight evidence.
    basis: str  # An explicitly supplied local-clock or timezone convention.
    role: str = 'occurrence'

    @classmethod
    def from_value(cls, value, precision, *, basis, role='occurrence'):
        steps = {'day':timedelta(days=1), 'minute':timedelta(minutes=1), 'second':timedelta(seconds=1)}
        if precision not in steps or not basis:
            raise ValueError('Explicit precision and time basis required')
        start = datetime.fromisoformat(value)
        if precision == 'day':
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        elif precision == 'minute':
            start = start.replace(second=0, microsecond=0)
        else:
            start = start.replace(microsecond=0)
        return cls(start, start + steps[precision], basis, role)


@dataclass(frozen=True)
class Observation:
    id: str
    facts: dict[str, Fact]
    # Display information is never matching evidence.
    label: str = ''

    def fact(self, key):
        return self.facts.get(key, Fact())


@dataclass(frozen=True)
class Policy:
    clock_skew_seconds: int = 0
    max_observations: int = 500
    max_component: int = 64

    def __post_init__(self):
        if (type(self.clock_skew_seconds) is not int or self.clock_skew_seconds < 0
                or self.max_observations < 1 or self.max_component < 1):
            raise ValueError('Invalid policy limits')


@dataclass(frozen=True)
class Coverage:
    # Caller must establish these for the WHOLE supplied scope, not a UI page.
    complete: bool = False
    scope_reference: str = ''
    blocked_records: frozenset[str] = field(default_factory=frozenset)


def decimal_amount(value):
    if isinstance(value, (float, bool)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (ValueError, InvalidOperation):
        return None


def assess_pair(a, b, policy=Policy()):
    """Separate contradictions from missing evidence, with auditable reason codes."""
    conflicts, missing, support = [], [], []
    def values(key):
        fa, fb = a.fact(key), b.fact(key)
        if fa.conflict or fb.conflict:
            conflicts.append(key + '_evidence_conflict')
        if not fa.known or not fb.known:
            missing.append(key + '_unknown')
            return None
        return fa.value, fb.value

    roles = values('role')
    if roles and set(roles) != {'account_ledger','payment_platform'}:
        conflicts.append('roles_not_complementary')
    amounts = values('amount')
    if amounts:
        amounts = tuple(decimal_amount(v) for v in amounts)
        if None in amounts: conflicts.append('amount_invalid')
        elif amounts[0] != amounts[1]: conflicts.append('amount_mismatch')
    for key in ('currency','direction','event_kind'):
        pair = values(key)
        if pair:
            if pair[0] != pair[1]: conflicts.append(key + '_mismatch')
            if key == 'currency' and any(not re.fullmatch(r'[A-Z]{3}', str(v)) for v in pair):
                conflicts.append('currency_invalid')
            if key == 'direction' and any(v not in {'in','out'} for v in pair):
                conflicts.append('direction_not_cash')
            if key == 'event_kind' and any(v not in {'payment','refund','transfer','topup','withdrawal'} for v in pair):
                missing.append('event_kind_unresolved')
    cash = values('cash_effect')
    if cash and any(v is not True for v in cash): conflicts.append('not_posted_cash')
    spans = values('time')
    if spans:
        x,y = spans
        if not all(isinstance(v, TimeRange) and v.start < v.end and v.basis for v in spans):
            conflicts.append('time_invalid')
        elif x.basis != y.basis or x.role != 'occurrence' or y.role != 'occurrence':
            missing.append('time_basis_or_role_unresolved')
        else:
            try:
                tolerance = timedelta(seconds=policy.clock_skew_seconds)
                if x.start >= y.end + tolerance or y.start >= x.end + tolerance:
                    conflicts.append('time_disjoint')
            except TypeError:
                missing.append('time_timezone_unresolved')
    # References are (namespace, value) pairs supplied by an adapter, not raw IDs.
    refs = [o.fact('references') for o in (a,b)]
    def refset(f):
        if not f.known or not isinstance(f.value, (tuple,list)): return set()
        return {tuple(v) for v in f.value if isinstance(v,(tuple,list)) and len(v)==2
                and all(isinstance(s,str) and s for s in v)}
    shared_reference = bool(refset(refs[0]) & refset(refs[1]))
    for f in refs:
        if f.conflict: conflicts.append('reference_evidence_conflict')
    channel_equal = True
    for key in ('funding_account','processor'):
        fa,fb = a.fact(key),b.fact(key)
        if fa.conflict or fb.conflict: conflicts.append(key+'_evidence_conflict')
        if fa.known and fb.known:
            if fa.value != fb.value:
                conflicts.append(key+'_mismatch')
                channel_equal = False
        else: channel_equal = False
    if shared_reference: support.append('namespaced_reference')
    if channel_equal: support.append('bilateral_account_and_processor')
    if not support: missing.append('identity_link_unknown')
    return {'ids':tuple(sorted((a.id,b.id))),
            'state':'rejected' if conflicts else 'needs_evidence' if missing else 'supported',
            'conflicts':sorted(set(conflicts)), 'missing':sorted(set(missing)), 'support':support}


def _matching(left, right, edges, forbidden=None):
    """Existence only. An arbitrary matching is never returned as the real mapping."""
    matched = {}
    def visit(a, seen):
        for b in sorted(right):
            if (a,b) not in edges or (a,b) == forbidden or b in seen: continue
            seen.add(b)
            if b not in matched or visit(matched[b], seen):
                matched[b] = a
                return True
        return False
    if all(visit(a,set()) for a in sorted(left)):
        return {(a,b) for b,a in matched.items()}
    return None


def propose_groups(observations, coverage=Coverage(), policy=Policy()):
    """Exhaustive within supplied scope. No truncation and no automatic adoption.

    A balanced component is a GROUP REVIEW proposal, not proof of duplication.
    Ambiguous record-to-record attribution remains explicit after group approval.
    """
    rows = {o.id:o for o in observations}
    if len(rows) != len(observations): raise ValueError('Duplicate observation IDs')
    if len(rows) > policy.max_observations:
        raise ValueError('Scope exceeds exhaustive prototype limit; do not use partial results')
    assessments = [assess_pair(a,b,policy) for a,b in combinations(rows.values(),2)]
    adjacency = {rid:set() for rid in rows}
    uncertain = []
    for p in assessments:
        a,b = p['ids']
        if p['state'] == 'supported':
            adjacency[a].add(b); adjacency[b].add(a)
        elif p['state'] == 'needs_evidence': uncertain.append((a,b))
    groups, seen = [], set()
    for seed in sorted(rows):
        if seed in seen or not adjacency[seed]: continue
        component, todo = set(), [seed]
        while todo:
            rid = todo.pop()
            if rid in component: continue
            component.add(rid); todo.extend(adjacency[rid]-component)
        seen.update(component)
        left = {rid for rid in component if rows[rid].fact('role').value=='account_ledger'}
        right = component-left
        reasons = []
        if not coverage.complete or not coverage.scope_reference: reasons.append('coverage_unconfirmed')
        if component & coverage.blocked_records: reasons.append('existing_decision_requires_review')
        if any(a in component or b in component for a,b in uncertain): reasons.append('unresolved_competitor')
        if len(left) != len(right): reasons.append('multiplicity_mismatch')
        if len(component) > policy.max_component: reasons.append('component_limit')
        if len(component)>2 and len({rows[r].fact('time').value.start.date() for r in component})>1:
            reasons.append('group_crosses_dates')
        edges = {(a,b) for a in left for b in adjacency[a]}
        matching = _matching(left,right,edges) if not reasons else None
        if not matching and not reasons: reasons.append('no_full_matching')
        forced = sorted(e for e in (matching or ()) if _matching(left,right,edges,forbidden=e) is None)
        total = sum((decimal_amount(rows[r].fact('amount').value) for r in left), Decimal(0))
        groups.append({'source_ids':sorted(component), 'account_rows':sorted(left),
                       'platform_rows':sorted(right), 'state':'blocked' if reasons else 'group_review',
                       'reasons':reasons, 'forced_pairs':[list(e) for e in forced],
                       'assignment':'unique' if matching and len(forced)==len(left) else 'unresolved',
                       'retained_event_count_if_confirmed':len(right) if not reasons else None,
                       'duplicate_amount_if_confirmed':format(total,'f') if not reasons else None,
                       'currency':rows[seed].fact('currency').value,
                       'direction':rows[seed].fact('direction').value,
                       'event_kind':rows[seed].fact('event_kind').value,
                       'field_attribution':('unresolved' if reasons else 'group_only'
                                            if len(forced)<len(left) else 'pair_if_confirmed'),
                       'automatic':False})
    return {'version':VERSION, 'scope_reference':coverage.scope_reference,
            'groups':groups, 'pairs':assessments, 'writes_book':False}
