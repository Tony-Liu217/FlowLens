"""Conservative, auditable field proposals from same-page OCR observations.

Two processed views are correlated evidence, NOT two independent models.
All output stays needs_review; no function here writes a ledger or marks ready.
"""
from __future__ import annotations

from copy import deepcopy

FIELDS = ("transaction_at", "amount", "direction", "balance")


def match_rows(base, other):
    """One-to-one geometric correspondence, never truth/value-based alignment.

    An extra/missing/shifted row rejects the whole route. Repeated date strings
    are therefore harmless, but unsupported rows cannot quietly disappear.
    """
    if len(base) != len(other):
        return None
    matches = []
    used = set()
    for row in base:
        anchor = row.get("source_geometry", {})
        if not anchor:
            return None
        candidates = []
        for i, candidate in enumerate(other):
            geom = candidate.get("source_geometry", {})
            if not geom:
                continue
            tolerance = min(anchor["row_height"], geom["row_height"]) * .3
            if abs(anchor["date_y"] - geom["date_y"]) <= tolerance and abs(anchor["date_x"] - geom["date_x"]) <= max(anchor["date_width"], geom["date_width"]) * .5:
                candidates.append(i)
        if len(candidates) != 1 or candidates[0] in used:
            return None
        used.add(candidates[0])
        matches.append(other[candidates[0]])
    return matches


def merge_observations(observations, page_issues, cell_evidence=None):
    """Preserve base values; fill only empty fields corroborated by two views.

    Any competing non-null interpretation clears the field in the proposal,
    while every observation (including original) remains in field_evidence.
    Amount and direction are handled as one semantic pair.
    """
    base = observations.get("original", [])
    issues = list(page_issues.get("original", []))
    if not base:
        return [], list(dict.fromkeys(issues + ["ORIGINAL_LAYOUT_REQUIRED"]))
    aligned = {"original": base}
    for route, records in observations.items():
        if route == "original":
            continue
        match = match_rows(base, records) if not page_issues.get(route) else None
        if match is None:
            issues.append("VIEW_LAYOUT_MISMATCH:" + route)
        else:
            aligned[route] = match
    proposals = []
    for i, original in enumerate(base):
        result = deepcopy(original)
        result["parse_status"] = "needs_review"
        result["baseline_issues"] = list(original["issues"])
        result["field_evidence"] = {}
        result["changes"] = []
        # Recompute basic field issues later; retain original ones in audit.
        result["issues"] = ["OCR_REQUIRES_REVIEW"]
        for group in [("transaction_at",), ("amount", "direction"), ("balance",)]:
            evidence = {name: {f: rows[i].get(f) for f in group} for name, rows in aligned.items()}
            for name, fields in (cell_evidence or {}).get(i, {}).get("+".join(group), {}).items():
                evidence[name] = {f: fields.get(f) for f in group}
            result["field_evidence"]["+".join(group)] = evidence
            values = {}
            for name, fields in evidence.items():
                value = tuple(fields[f] for f in group)
                if all(v is not None for v in value):
                    values.setdefault(value, []).append(name)
            before = tuple(original.get(f) for f in group)
            # Even a partial original pair (amount known, sign unreadable) is
            # evidence. Do not silently change that known amount using a pair
            # supplied by processed views.
            partial_conflict = any(
                len({fields[f] for fields in evidence.values() if fields[f] is not None}) > 1
                for f in group
            )
            if len(values) > 1 or partial_conflict:
                after = (None,) * len(group)
                result["issues"].append("OCR_VIEW_CONFLICT:" + "+".join(group))
            elif all(v is not None for v in before):
                after = before
                if any(any(v is None for v in fields.values()) for fields in evidence.values()):
                    result["issues"].append("VIEW_LOST_FIELD:" + "+".join(group))
            elif values and len(next(iter(values.values()))) >= 2:
                after = next(iter(values))
                result["issues"].append("RECOVERED_FROM_DERIVED_VIEWS:" + "+".join(group))
            else:
                after = before
                result["issues"].append("UNRESOLVED_FIELD:" + "+".join(group))
            for f, value in zip(group, after):
                result[f] = value
            if before != after:
                result["changes"].append({"fields": list(group), "before": list(before), "after": list(after)})
        proposals.append(result)
    return proposals, list(dict.fromkeys(issues))
